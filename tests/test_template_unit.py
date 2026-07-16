"""Templates — the namespaced resource whose payload, unlike a secret's, comes back.

The fake below is dict-backed and mimics only what `client.py` really does that
these tests depend on: `NotFoundError` for a missing row, `DryRun` instead of a
write when dry-running, and the plain-text body a *wrong /api path* returns (the
real client hands back a string rather than exploding in json.loads — that is
how the unverified item route is allowed to not exist).

Shapes are the ones observed live during the 2026-07-16 spike:
`{"id":1,"name":"t.yml","namespace":"acme","data":"kind: pipeline"}` — note the
`data`, which is exactly what a secret never returns.
"""

from __future__ import annotations

import json

import pytest
from agentcli import Emitter, OutputFormat
from agentcli.errors import ConfigError, DryRun, NotFoundError, OpError, ValidationError
from typer.testing import CliRunner

from dronecli.commands import template

NS = "acme"
SLUG = "droneadmin/linktest"
YAML = "kind: pipeline\nname: default\nsteps:\n  - name: test\n    commands:\n      - go test ./...\n"

runner = CliRunner()


# ---- fakes ------------------------------------------------------------


class FakeClient:
    """A dict-backed stand-in for dronecli.client.Client."""

    def __init__(self, store: dict | None = None, *, dry_run: bool = False, item_route: bool = True):
        self.store = dict(store or {})
        self.dry_run = dry_run
        #: False models a server where GET /api/templates/{ns}/{name} is not
        #: routed at all -- the case the spike never got to verify.
        self.item_route = item_route
        self.calls: list[tuple[str, str, dict | None]] = []

    def _record(self, method, path, json=None):
        self.calls.append((method, path, json))

    @staticmethod
    def _is_collection(path: str) -> bool:
        return path.startswith("templates/") and path.count("/") == 1

    def get(self, path, **kw):
        self._record("GET", path)
        if self._is_collection(path):
            return list(self.store.values())
        if not self.item_route:
            # What client.py really returns for an unrouted /api path: drone
            # answers "404 page not found" as text/plain, status 404.
            raise NotFoundError("404 page not found")
        name = path.rpartition("/")[2]
        if name not in self.store:
            raise NotFoundError("sql: no rows in result set")
        return self.store[name]

    def _write(self, method, path, json=None):
        self._record(method, path, json)
        if self.dry_run:
            raise DryRun({"method": method, "url": f"http://drone/api/{path}", "body": json})

    def post(self, path, *, json=None, **kw):
        self._write("POST", path, json)
        name = json["name"]
        row = {"id": 1, "name": name, "namespace": path.rpartition("/")[2], "data": json["data"]}
        self.store[name] = row
        return row

    def patch(self, path, *, json=None, **kw):
        self._write("PATCH", path, json)
        name = path.rpartition("/")[2]
        if name not in self.store:
            raise NotFoundError("sql: no rows in result set")
        row = {**self.store[name], "data": json["data"]}
        self.store[name] = row
        return row

    def delete(self, path, **kw):
        self._write("DELETE", path, None)
        self.store.pop(path.rpartition("/")[2], None)
        return None


class FakeConfig:
    def __init__(self, context: dict | None = None):
        self.context = context or {}


class FakeObj:
    """Stands in for AppContext."""

    def __init__(self, client: FakeClient, *, fields=None, context=None):
        self._client = client
        self.config = FakeConfig(context)
        self.emitter = Emitter(OutputFormat.json, color=False, fields=fields)

    def client(self):
        return self._client


def mk(name="t.yml", data=YAML, namespace=NS):
    return {"id": 1, "name": name, "namespace": namespace, "data": data}


def invoke(args, obj):
    return runner.invoke(template.app, args, obj=obj, catch_exceptions=True)


def out_json(result):
    return json.loads(result.stdout)


# ---- the asymmetry with secrets: data IS readable ----------------------


def test_get_returns_the_yaml_body():
    """The headline difference from `secret get`, which can never show a value.
    If this ever regresses to null, --out and the whole round-trip die with it."""
    res = invoke(["get", "t.yml", "--org", NS], FakeObj(FakeClient({"t.yml": mk()})))
    assert res.exit_code == 0, res.output
    got = out_json(res)
    assert got["data"] == YAML
    assert got["name"] == "t.yml"


def test_get_says_out_loud_that_this_is_not_a_secret():
    """Someone arriving from secret.py 'knows' the payload is write-only. The one
    place to correct that belief is the output they are already reading."""
    res = invoke(["get", "t.yml", "--org", NS], FakeObj(FakeClient({"t.yml": mk()})))
    assert "unlike secrets" in out_json(res)["note"]


# ---- ls ---------------------------------------------------------------


def test_ls_omits_the_body_but_keeps_its_size():
    """Ten templates is ten YAML documents; `ls` answers 'what exists?'. The size
    fields keep 'is this the empty one?' answerable without a second call."""
    client = FakeClient({"a.yml": mk("a.yml"), "b.yml": mk("b.yml", data="kind: pipeline")})
    res = invoke(["ls", "--org", NS], FakeObj(client))
    rows = out_json(res)
    assert [r["name"] for r in rows] == ["a.yml", "b.yml"]
    assert not any("data" in r for r in rows), "the body must not flood a listing"
    assert rows[0]["data_lines"] == 6 and rows[0]["data_bytes"] == len(YAML.encode())
    assert client.calls == [("GET", f"templates/{NS}", None)]


def test_ls_with_data_opts_back_in():
    res = invoke(["ls", "--org", NS, "--with-data"], FakeObj(FakeClient({"a.yml": mk("a.yml")})))
    assert out_json(res)[0]["data"] == YAML


def test_ls_of_an_empty_namespace_is_an_empty_list_not_an_error():
    assert out_json(invoke(["ls", "--org", NS], FakeObj(FakeClient()))) == []


# ---- the namespaced URL tree ------------------------------------------


def test_every_call_is_namespaced_never_the_bare_collection():
    """POST /api/templates is a 405, not a 404 — 'wrong verb, right URL' sends
    you hunting for a verb that does not exist. The namespace is not optional."""
    client = FakeClient({"t.yml": mk()})
    obj = FakeObj(client)
    invoke(["ls", "--org", NS], obj)
    invoke(["get", "t.yml", "--org", NS], obj)
    invoke(["rm", "t.yml", "-y", "--org", NS], obj)
    paths = [c[1] for c in client.calls]
    assert paths == [f"templates/{NS}", f"templates/{NS}/t.yml", f"templates/{NS}/t.yml"]
    assert "templates" not in paths, "the bare collection 405s"


def test_add_posts_to_the_namespace(tmp_path):
    p = tmp_path / "deploy.yml"
    p.write_text(YAML)
    client = FakeClient()
    res = invoke(["add", "deploy.yml", "--from-file", str(p), "--org", NS], FakeObj(client))
    assert res.exit_code == 0, res.output
    assert client.calls[0][:2] == ("POST", f"templates/{NS}")
    assert client.calls[0][2] == {"name": "deploy.yml", "data": YAML}
    assert out_json(res)["action"] == "created"


# ---- --org resolution (the shared rule, not a copy of it) -------------


def test_org_defaults_from_the_context_owner():
    obj = FakeObj(FakeClient(), context={"owner": NS})
    invoke(["ls"], obj)
    assert obj._client.calls[0][1] == f"templates/{NS}"


def test_org_falls_back_to_the_namespace_half_of_a_sticky_repo():
    """`context set --repo droneadmin/linktest` has already said 'droneadmin'."""
    obj = FakeObj(FakeClient(), context={"repo": SLUG})
    invoke(["ls"], obj)
    assert obj._client.calls[0][1] == "templates/droneadmin"


def test_namespace_is_an_alias_for_org():
    obj = FakeObj(FakeClient())
    invoke(["ls", "--namespace", NS], obj)
    assert obj._client.calls[0][1] == f"templates/{NS}"


def test_no_org_anywhere_explains_itself():
    res = invoke(["ls"], FakeObj(FakeClient()))
    assert isinstance(res.exception, ConfigError)
    assert "--org" in res.exception.message


def test_org_rejects_a_repo_slug():
    res = invoke(["ls", "--org", SLUG], FakeObj(FakeClient()))
    assert isinstance(res.exception, ConfigError)


# ---- --out: the flag that must not be called --output ------------------


def test_out_writes_the_body_to_a_file(tmp_path):
    dest = tmp_path / "deploy.yml"
    res = invoke(["get", "t.yml", "--org", NS, "--out", str(dest)], FakeObj(FakeClient({"t.yml": mk()})))
    assert res.exit_code == 0, res.output
    assert dest.read_text() == YAML


def test_out_writes_the_named_file_not_a_format_degraded_guess(tmp_path):
    """The OpenProject bug this flag is named around: `--output f.pdf` was eaten
    by the reserved global, degraded to json, and wrote a differently-named file
    to the CWD with exit 0. The path asked for must be the path written."""
    dest = tmp_path / "sub" / "exact-name.yml"
    res = invoke(["get", "t.yml", "--org", NS, "--out", str(dest)], FakeObj(FakeClient({"t.yml": mk()})))
    assert res.exit_code == 0, res.output
    assert dest.is_file(), "the exact path (incl. parents) must exist"
    assert out_json(res)["path"] == str(dest)
    assert list(tmp_path.rglob("*.yml")) == [dest], "nothing may land anywhere else"


def test_out_reports_where_it_went_instead_of_echoing_the_body(tmp_path):
    dest = tmp_path / "deploy.yml"
    res = invoke(["get", "t.yml", "--org", NS, "--out", str(dest)], FakeObj(FakeClient({"t.yml": mk()})))
    got = out_json(res)
    assert "data" not in got, "the body is in the file; doubling it wastes every byte"
    assert got["action"] == "written" and got["data_bytes"] == len(YAML.encode())


def test_out_dash_is_refused_rather_than_writing_a_file_called_dash(tmp_path):
    """Raw YAML on stdout would corrupt the JSON envelope an agent is parsing."""
    res = invoke(["get", "t.yml", "--org", NS, "--out", "-"], FakeObj(FakeClient({"t.yml": mk()})))
    assert isinstance(res.exception, OpError)
    assert not (tmp_path / "-").exists()


def test_out_round_trips_byte_for_byte(tmp_path):
    """add --from-file then get --out must reproduce the original file exactly —
    the thing secrets can never do."""
    src = tmp_path / "src.yml"
    src.write_text(YAML)
    client = FakeClient()
    invoke(["add", "t.yml", "--from-file", str(src), "--org", NS], FakeObj(client))
    dest = tmp_path / "dest.yml"
    invoke(["get", "t.yml", "--org", NS, "--out", str(dest)], FakeObj(client))
    assert dest.read_text() == src.read_text()


# ---- the ergonomic win: JSON-escaping the YAML ------------------------


def test_from_file_escapes_the_yaml_into_the_data_string(tmp_path):
    """The point of the whole module: the API wants a whole YAML document
    JSON-escaped into one string, which is the worst thing to do by hand."""
    nasty = 'kind: pipeline\nname: "quoted: value"\ncommands:\n  - echo "hi\\there"\n\ttrailing\n'
    p = tmp_path / "n.yml"
    p.write_text(nasty)
    client = FakeClient()
    invoke(["add", "n.yml", "--from-file", str(p), "--org", NS], FakeObj(client))
    assert client.calls[0][2]["data"] == nasty, "verbatim, escaping left to the JSON encoder"


def test_the_body_is_sent_verbatim_without_stripping_a_trailing_newline(tmp_path):
    """secret.read_value strips one trailing newline — a token-specific rule. A
    template is a file: stripping it would break the round-trip above."""
    p = tmp_path / "t.yml"
    p.write_text(YAML)
    client = FakeClient()
    invoke(["add", "t.yml", "--from-file", str(p), "--org", NS], FakeObj(client))
    assert client.calls[0][2]["data"].endswith("\n")


def test_from_stdin(monkeypatch):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(YAML))
    assert template.read_data(None, True) == YAML


def test_exactly_one_body_source_required():
    with pytest.raises(OpError, match="no template body"):
        template.read_data(None, False)
    with pytest.raises(OpError, match="exactly one"):
        template.read_data("f.yml", True)


def test_empty_body_is_refused(tmp_path):
    """An empty template stores fine and then fails at build time, where the
    error names the pipeline instead of this command."""
    p = tmp_path / "empty.yml"
    p.write_text("   \n")
    with pytest.raises(ValidationError, match="empty"):
        template.read_data(str(p), False)


def test_from_file_missing_is_a_clean_error(tmp_path):
    with pytest.raises(OpError):
        template.read_data(str(tmp_path / "nope.yml"), False)


# ---- update -----------------------------------------------------------


def test_update_patches_data_only(tmp_path):
    p = tmp_path / "new.yml"
    p.write_text("kind: pipeline\nname: updated\n")
    client = FakeClient({"t.yml": mk()})
    res = invoke(["update", "t.yml", "--from-file", str(p), "--org", NS], FakeObj(client))
    assert res.exit_code == 0, res.output
    assert client.calls[0][:2] == ("PATCH", f"templates/{NS}/t.yml")
    assert client.calls[0][2] == {"data": "kind: pipeline\nname: updated\n"}, \
        "name is the URL, not the body"
    assert out_json(res)["action"] == "updated"


def test_update_of_a_missing_template_is_a_notfound(tmp_path):
    p = tmp_path / "n.yml"
    p.write_text(YAML)
    res = invoke(["update", "nope.yml", "--from-file", str(p), "--org", NS], FakeObj(FakeClient()))
    assert isinstance(res.exception, NotFoundError)


# ---- the unverified item route ----------------------------------------


def test_get_falls_back_to_the_verified_collection_when_the_item_route_404s():
    """GET /api/templates/{ns}/{name} was never exercised live. If it does not
    exist, the collection — which IS verified and returns `data` — answers. This
    must not surface as 'no such template'."""
    client = FakeClient({"t.yml": mk()}, item_route=False)
    res = invoke(["get", "t.yml", "--org", NS], FakeObj(client))
    assert res.exit_code == 0, res.output
    assert out_json(res)["data"] == YAML
    assert [c[1] for c in client.calls] == [f"templates/{NS}/t.yml", f"templates/{NS}"]


def test_get_prefers_the_item_route_and_does_not_list_when_it_works():
    """The fallback must stay a fallback: one GET on the happy path."""
    client = FakeClient({"t.yml": mk()})
    invoke(["get", "t.yml", "--org", NS], FakeObj(client))
    assert [c[1] for c in client.calls] == [f"templates/{NS}/t.yml"]


def test_a_genuinely_missing_template_still_raises_after_the_fallback():
    client = FakeClient({"other.yml": mk("other.yml")}, item_route=False)
    res = invoke(["get", "nope.yml", "--org", NS], FakeObj(client))
    assert isinstance(res.exception, NotFoundError)
    assert "template ls" in res.exception.message, "point at the command that lists them"


# ---- rm ---------------------------------------------------------------


def test_rm_requires_yes_or_a_confirmation():
    client = FakeClient({"t.yml": mk()})
    res = runner.invoke(template.app, ["rm", "t.yml", "--org", NS], obj=FakeObj(client), input="n\n")
    assert res.exit_code != 0
    assert not [c for c in client.calls if c[0] == "DELETE"], "aborting must not delete"
    assert "t.yml" in client.store


def test_rm_with_yes_deletes():
    client = FakeClient({"t.yml": mk()})
    res = invoke(["rm", "t.yml", "-y", "--org", NS], FakeObj(client))
    assert res.exit_code == 0, res.output
    assert client.calls[-1][:2] == ("DELETE", f"templates/{NS}/t.yml")
    assert out_json(res) == {"status": "deleted", "namespace": NS, "name": "t.yml"}


# ---- push -------------------------------------------------------------


def test_push_uploads_every_yml_named_after_the_file(tmp_path):
    (tmp_path / "a.yml").write_text(YAML)
    (tmp_path / "b.yml").write_text("kind: pipeline\nname: b\n")
    (tmp_path / "notes.txt").write_text("not a template")
    client = FakeClient()
    res = invoke(["push", str(tmp_path), "--org", NS], FakeObj(client))
    assert res.exit_code == 0, res.output
    rows = out_json(res)
    assert [r["name"] for r in rows] == ["a.yml", "b.yml"], "only *.yml, sorted"
    assert all(r["action"] == "created" for r in rows)
    assert sorted(client.store) == ["a.yml", "b.yml"]


def test_push_is_idempotent_and_reports_create_vs_update(tmp_path):
    """Re-running over a directory kept in git must be safe — that is the point."""
    (tmp_path / "a.yml").write_text(YAML)
    client = FakeClient()
    first = invoke(["push", str(tmp_path), "--org", NS], FakeObj(client))
    second = invoke(["push", str(tmp_path), "--org", NS], FakeObj(client))
    assert out_json(first)[0]["action"] == "created"
    assert out_json(second)[0]["action"] == "updated"
    assert [c[0] for c in client.calls] == ["GET", "POST", "GET", "PATCH"]


def test_push_skips_an_empty_file_without_failing_the_batch(tmp_path):
    """Storing an empty template would fail later at build time; aborting the
    whole run over one stray file would be worse."""
    (tmp_path / "empty.yml").write_text("\n")
    (tmp_path / "good.yml").write_text(YAML)
    client = FakeClient()
    rows = out_json(invoke(["push", str(tmp_path), "--org", NS], FakeObj(client)))
    assert {r["name"]: r["action"] for r in rows} == {"empty.yml": "skipped", "good.yml": "created"}
    assert "empty.yml" not in client.store


def test_push_glob_reaches_the_other_engines(tmp_path):
    (tmp_path / "a.star").write_text("def main(ctx):\n  return []\n")
    (tmp_path / "b.yml").write_text(YAML)
    client = FakeClient()
    rows = out_json(invoke(["push", str(tmp_path), "--org", NS, "--glob", "*.star"], FakeObj(client)))
    assert [r["name"] for r in rows] == ["a.star"]


def test_push_recovers_when_a_template_is_created_between_probe_and_post(tmp_path):
    """Drone has no 409 — a uniqueness collision arrives as 400/ValidationError.
    Catching only ConflictError would leave the race unhandled."""
    class Racy(FakeClient):
        def post(self, path, *, json=None, **kw):
            self._record("POST", path, json)
            self.store[json["name"]] = mk(json["name"])
            raise ValidationError("template already exists")

    (tmp_path / "a.yml").write_text(YAML)
    client = Racy()
    res = invoke(["push", str(tmp_path), "--org", NS], FakeObj(client))
    assert res.exit_code == 0, res.output
    assert out_json(res)[0]["action"] == "updated"
    assert [c[0] for c in client.calls] == ["GET", "POST", "PATCH"]


def test_push_recovers_when_a_template_is_deleted_between_probe_and_patch(tmp_path):
    class Racy(FakeClient):
        def patch(self, path, *, json=None, **kw):
            self._record("PATCH", path, json)
            raise NotFoundError("sql: no rows in result set")

    (tmp_path / "a.yml").write_text(YAML)
    client = Racy({"a.yml": mk("a.yml")})
    res = invoke(["push", str(tmp_path), "--org", NS], FakeObj(client))
    assert res.exit_code == 0, res.output
    assert out_json(res)[0]["action"] == "created"
    assert [c[0] for c in client.calls] == ["GET", "PATCH", "POST"]


def test_push_is_not_a_sync_it_never_deletes(tmp_path):
    """A file deleted locally must not delete the template: removing one is a
    blast-radius decision that stays explicit."""
    (tmp_path / "a.yml").write_text(YAML)
    client = FakeClient({"gone.yml": mk("gone.yml")})
    invoke(["push", str(tmp_path), "--org", NS], FakeObj(client))
    assert "gone.yml" in client.store
    assert not [c for c in client.calls if c[0] == "DELETE"]


def test_push_at_a_file_points_at_add_instead(tmp_path):
    p = tmp_path / "a.yml"
    p.write_text(YAML)
    res = invoke(["push", str(p), "--org", NS], FakeObj(FakeClient()))
    assert isinstance(res.exception, OpError)
    assert "template add" in res.exception.message


def test_push_of_an_empty_directory_says_so(tmp_path):
    res = invoke(["push", str(tmp_path), "--org", NS], FakeObj(FakeClient()))
    assert isinstance(res.exception, OpError)
    assert "--glob" in res.exception.message


# ---- dry run (the transport's job, not ours) --------------------------


@pytest.mark.parametrize("args", [
    ["add", "t.yml", "--org", NS],
    ["update", "t.yml", "--org", NS],
])
def test_writes_are_dry_runnable(args, tmp_path):
    p = tmp_path / "f.yml"
    p.write_text(YAML)
    client = FakeClient({"t.yml": mk()}, dry_run=True)
    res = invoke([*args, "--from-file", str(p)], FakeObj(client))
    assert isinstance(res.exception, DryRun)
    assert res.exception.request["body"]["data"] == YAML


def test_dry_run_rm_does_not_delete():
    client = FakeClient({"t.yml": mk()}, dry_run=True)
    res = invoke(["rm", "t.yml", "-y", "--org", NS], FakeObj(client))
    assert isinstance(res.exception, DryRun)
    assert "t.yml" in client.store


def test_the_yaml_body_is_not_redacted_in_a_dry_run(tmp_path):
    """The inverse of the secrets rule, and the reason this module has no
    redaction chokepoint: a template is not confidential, and hiding the body
    would make --dry-run useless for the one command that needs it most."""
    p = tmp_path / "f.yml"
    p.write_text(YAML)
    client = FakeClient(dry_run=True)
    res = invoke(["add", "t.yml", "--from-file", str(p), "--org", NS], FakeObj(client))
    assert res.exception.request["body"]["data"] == YAML
    assert secret_module_would_have_redacted() not in json.dumps(res.exception.request)


def secret_module_would_have_redacted() -> str:
    """The placeholder `secret.py` substitutes for a value under --dry-run.
    Imported through a function so this file states the contrast it is testing:
    that placeholder must never appear in a template's request."""
    from dronecli.commands.secret import REDACTED

    return REDACTED


# ---- names ------------------------------------------------------------


@pytest.mark.parametrize("name", ["deploy.yml", "a.star", "plain", "with-dash_and.dot.yaml"])
def test_valid_names(name):
    assert template.validate_name(name) == name


@pytest.mark.parametrize("name", ["", "   ", "a/b.yml", "sub/dir/t.yml"])
def test_names_that_would_break_the_url_are_refused(name):
    with pytest.raises(ValidationError):
        template.validate_name(name)


def test_fields_projection_still_works():
    obj = FakeObj(FakeClient({"a.yml": mk("a.yml")}), fields=["name"])
    res = invoke(["ls", "--org", NS], obj)
    assert res.exit_code == 0, res.output
    assert out_json(res) == [{"name": "a.yml"}]


# ---- the reserved-globals rule, locally -------------------------------


def test_no_template_command_declares_a_reserved_global():
    """--out, never --output. `_pop_globals` strips the reserved globals from
    anywhere on the line, so a local --output could never be received: the path
    would be swallowed as an output *format*. The tree-wide test in
    test_globals_unit.py enforces this too; assert it here so this module fails
    on its own terms."""
    from typer.main import get_command

    reserved = {"--output", "-o", "--format", "-f", "--fields", "--columns",
                "--dry-run", "--stream", "--no-context"}
    for name, cmd in get_command(template.app).commands.items():
        for param in cmd.params:
            assert not (set(param.opts) & reserved), f"{name} declares {param.opts}"
