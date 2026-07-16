"""Secrets — the write-only resource. Pure: no network, no server.

The fake below mimics exactly two behaviours of `client.py` that these tests
depend on, and nothing else: it raises `NotFoundError` for a missing secret, and
in dry-run mode it raises `DryRun` carrying the body it was **handed** instead of
performing the write. That second one is the whole point — the redaction has to
happen at the call site *before* the client sees the body, because the real
interceptor prints whatever it is given and cannot know which field is a secret.

Shapes are the ones observed live during the 2026-07-16 spike: a secret reads
back as `{id, repo_id, name}` (+ the pull_request flags) — never a value.
"""

from __future__ import annotations

import json

import pytest
from agentcli import Emitter, OutputFormat
from agentcli.errors import ConfigError, DryRun, NotFoundError, OpError, ValidationError
from typer.testing import CliRunner

from dronecli.commands import orgsecret, secret

SLUG = "droneadmin/linktest"
VALUE = "hunter2-super-secret-value"

runner = CliRunner()


# ---- fakes ------------------------------------------------------------


class FakeClient:
    """A dict-backed stand-in for dronecli.client.Client."""

    def __init__(self, store: dict | None = None, *, dry_run: bool = False):
        self.store = dict(store or {})
        self.dry_run = dry_run
        self.calls: list[tuple[str, str, dict | None]] = []

    def _record(self, method, path, json=None):
        self.calls.append((method, path, json))

    @staticmethod
    def _is_collection(path: str) -> bool:
        # repos/{o}/{n}/secrets  and  secrets/{ns}  are the two list endpoints.
        return path.endswith("/secrets") or (path.startswith("secrets/") and path.count("/") == 1)

    def get(self, path, **kw):
        self._record("GET", path)
        if self._is_collection(path):
            return list(self.store.values())
        name = path.rpartition("/")[2]
        if name not in self.store:
            raise NotFoundError("sql: no rows in result set")
        return self.store[name]

    def _write(self, method, path, json=None):
        self._record(method, path, json)
        if self.dry_run:
            # Mirrors the real chokepoint: writes never execute, the body is echoed.
            raise DryRun({"method": method, "url": f"http://drone/api/{path}", "body": json})

    def post(self, path, *, json=None, **kw):
        self._write("POST", path, json)
        name = json["name"]
        row = {"id": 7, "repo_id": 1, "name": name,
               "pull_request": json.get("pull_request", False),
               "pull_request_push": json.get("pull_request_push", False)}
        self.store[name] = row
        return row

    def patch(self, path, *, json=None, **kw):
        self._write("PATCH", path, json)
        name = path.rpartition("/")[2]
        if name not in self.store:
            raise NotFoundError("sql: no rows in result set")
        row = {**self.store[name], **{k: v for k, v in json.items() if k != "data"}}
        self.store[name] = row
        return row

    def delete(self, path, **kw):
        self._write("DELETE", path, None)
        name = path.rpartition("/")[2]
        self.store.pop(name, None)
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


def mk(name, pull_request=False, **kw):
    return {"id": 3, "repo_id": 1, "name": name, "pull_request": pull_request,
            "pull_request_push": False, **kw}


def invoke(app, args, obj):
    return runner.invoke(app, args, obj=obj, catch_exceptions=True)


def out_json(result):
    return json.loads(result.stdout)


# ---- the rule that matters: the value is never printed -----------------


def test_dry_run_redacts_the_value_on_create():
    """--dry-run is intercepted in client.py, which cannot know `data` is secret.
    If the value reached the client, a dry run would print the secret to stdout
    (and into CI logs). So the redaction must happen before the call."""
    client = FakeClient(dry_run=True)
    obj = FakeObj(client)
    res = invoke(secret.app, ["set", "token", "--value", VALUE, "-r", SLUG], obj)

    assert isinstance(res.exception, DryRun)
    body = res.exception.request["body"]
    assert body["data"] == secret.REDACTED
    assert VALUE not in json.dumps(res.exception.request)
    assert VALUE not in res.stdout


def test_dry_run_redacts_the_value_on_update_too():
    client = FakeClient({"token": mk("token")}, dry_run=True)
    res = invoke(secret.app, ["set", "token", "--value", VALUE, "-r", SLUG], FakeObj(client))
    assert isinstance(res.exception, DryRun)
    assert res.exception.request["method"] == "PATCH", "an existing secret dry-runs as a PATCH"
    assert res.exception.request["body"]["data"] == secret.REDACTED
    assert VALUE not in json.dumps(res.exception.request)


def test_dry_run_redacts_org_secrets_as_well():
    """Same rule, other tree — the two modules must not drift apart."""
    client = FakeClient(dry_run=True)
    res = invoke(orgsecret.app, ["set", "token", "--value", VALUE, "--org", "acme"], FakeObj(client))
    assert isinstance(res.exception, DryRun)
    assert res.exception.request["body"]["data"] == secret.REDACTED
    assert VALUE not in json.dumps(res.exception.request)


def test_the_value_never_appears_in_a_real_write_response():
    client = FakeClient()
    res = invoke(secret.app, ["set", "token", "--value", VALUE, "-r", SLUG], FakeObj(client))
    assert res.exit_code == 0, res.output
    assert VALUE not in res.stdout
    assert "data" not in out_json(res)


def test_redaction_is_not_applied_to_the_wire_when_not_dry_running():
    """The guard must not become a bug: a real write sends the REAL value."""
    client = FakeClient()
    invoke(secret.app, ["set", "token", "--value", VALUE, "-r", SLUG], FakeObj(client))
    posts = [c for c in client.calls if c[0] == "POST"]
    assert posts and posts[0][2]["data"] == VALUE


def test_shape_never_leaks_a_value_even_if_the_server_sends_one():
    """An allowlist, not `del row['data']`: this must not depend on the server
    continuing to blank the field, and `type` is decoded-then-discarded by Drone
    so exposing it would invent a knob that does nothing."""
    got = secret.shape({"id": 1, "name": "t", "data": "leaked", "type": "secret",
                        "pull_request": False})
    assert got == {"name": "t", "pull_request": False, "id": 1}


# ---- refusing to project the value ------------------------------------


@pytest.mark.parametrize("field", ["data", "data.value", "value"])
def test_fields_data_is_refused(field):
    """`_dotted_get` would happily emit `data: null`, which reads as "the secret
    is blank" — a lie about a secret that is set. Refuse instead of answering."""
    obj = FakeObj(FakeClient({"token": mk("token")}), fields=[field])
    res = invoke(secret.app, ["get", "token", "-r", SLUG], obj)
    assert res.exit_code != 0
    assert isinstance(res.exception, ValidationError)
    assert "write-only" in res.exception.message


def test_fields_data_is_refused_on_org_secrets_and_on_writes():
    obj = FakeObj(FakeClient(), fields=["data"])
    res = invoke(orgsecret.app, ["ls", "--org", "acme"], obj)
    assert isinstance(res.exception, ValidationError)

    obj2 = FakeObj(FakeClient(), fields=["data"])
    res2 = invoke(secret.app, ["set", "t", "--value", "x", "-r", SLUG], obj2)
    assert isinstance(res2.exception, ValidationError)


def test_normal_fields_still_work():
    obj = FakeObj(FakeClient({"a": mk("a")}), fields=["name"])
    res = invoke(secret.app, ["ls", "-r", SLUG], obj)
    assert res.exit_code == 0, res.output
    assert out_json(res) == [{"name": "a"}]


# ---- get: teaching "impossible" ---------------------------------------


def test_get_reports_data_null_with_a_reason_not_an_empty_string():
    """`""` would read as "the secret is blank". null + a note reads as
    "unreadable", which is the truth."""
    res = invoke(secret.app, ["get", "token", "-r", SLUG], FakeObj(FakeClient({"token": mk("token")})))
    got = out_json(res)
    assert got["data"] is None
    assert "write-only" in got["note"]
    assert got["name"] == "token"


def test_get_warns_when_the_secret_is_exposed_to_prs():
    res = invoke(secret.app, ["get", "t", "-r", SLUG], FakeObj(FakeClient({"t": mk("t", pull_request=True)})))
    assert "pull_request=true" in out_json(res)["warning"]


def test_get_missing_is_a_notfound():
    res = invoke(secret.app, ["get", "nope", "-r", SLUG], FakeObj(FakeClient()))
    assert isinstance(res.exception, NotFoundError)


# ---- the upsert ------------------------------------------------------


def test_set_creates_when_absent():
    client = FakeClient()
    res = invoke(secret.app, ["set", "token", "--value", VALUE, "-r", SLUG], FakeObj(client))
    assert out_json(res)["action"] == "created"
    assert [c[0] for c in client.calls] == ["GET", "POST"]
    assert client.calls[1][1] == f"repos/{SLUG}/secrets"


def test_set_updates_when_present():
    client = FakeClient({"token": mk("token")})
    res = invoke(secret.app, ["set", "token", "--value", VALUE, "-r", SLUG], FakeObj(client))
    assert out_json(res)["action"] == "updated"
    assert [c[0] for c in client.calls] == ["GET", "PATCH"]
    assert client.calls[1][1] == f"repos/{SLUG}/secrets/token"


def test_set_is_idempotent():
    """The whole reason `set` exists: the raw API forces create-404-then-patch."""
    client = FakeClient()
    obj = FakeObj(client)
    first = invoke(secret.app, ["set", "t", "--value", "a", "-r", SLUG], obj)
    second = invoke(secret.app, ["set", "t", "--value", "a", "-r", SLUG], obj)
    assert first.exit_code == 0 and second.exit_code == 0
    assert out_json(first)["action"] == "created"
    assert out_json(second)["action"] == "updated"


def test_set_recovers_when_the_secret_is_deleted_between_probe_and_patch():
    class Racy(FakeClient):
        def patch(self, path, *, json=None, **kw):
            self._record("PATCH", path, json)
            raise NotFoundError("sql: no rows in result set")

    client = Racy({"t": mk("t")})
    res = invoke(secret.app, ["set", "t", "--value", VALUE, "-r", SLUG], FakeObj(client))
    assert res.exit_code == 0, res.output
    assert out_json(res)["action"] == "created"
    assert [c[0] for c in client.calls] == ["GET", "PATCH", "POST"]


def test_set_recovers_when_the_secret_is_created_between_probe_and_post():
    """Drone has no 409 — a uniqueness collision arrives as 400/ValidationError.
    Catching only ConflictError here would leave the race unhandled."""
    class Racy(FakeClient):
        def post(self, path, *, json=None, **kw):
            self._record("POST", path, json)
            self.store["t"] = mk("t")
            raise ValidationError("Secret already exists")

    client = Racy()
    res = invoke(secret.app, ["set", "t", "--value", VALUE, "-r", SLUG], FakeObj(client))
    assert res.exit_code == 0, res.output
    assert out_json(res)["action"] == "updated"
    assert [c[0] for c in client.calls] == ["GET", "POST", "PATCH"]


def test_set_omits_unmentioned_flags_so_a_patch_cannot_clobber_them():
    client = FakeClient({"t": mk("t", pull_request=True)})
    invoke(secret.app, ["set", "t", "--value", VALUE, "-r", SLUG], FakeObj(client))
    body = client.calls[1][2]
    assert "pull_request" not in body, "not passing --pull-request must not silently turn it off"
    assert client.store["t"]["pull_request"] is True


def test_pull_request_flag_is_sent_and_warned_about():
    client = FakeClient()
    res = invoke(secret.app, ["set", "t", "--value", VALUE, "--pull-request", "-r", SLUG], FakeObj(client))
    assert client.calls[1][2]["pull_request"] is True
    assert "pull_request=true" in out_json(res)["warning"], "a security boundary must be said out loud"


def test_no_pull_request_is_distinguishable_from_unset():
    client = FakeClient({"t": mk("t", pull_request=True)})
    invoke(secret.app, ["set", "t", "--value", VALUE, "--no-pull-request", "-r", SLUG], FakeObj(client))
    assert client.calls[1][2]["pull_request"] is False


# ---- value sources ----------------------------------------------------


def test_from_env(monkeypatch):
    monkeypatch.setenv("MY_TOKEN", VALUE)
    client = FakeClient()
    res = invoke(secret.app, ["set", "t", "--from-env", "MY_TOKEN", "-r", SLUG], FakeObj(client))
    assert res.exit_code == 0, res.output
    assert client.calls[1][2]["data"] == VALUE


def test_from_env_unset_is_an_error_not_an_empty_secret():
    """Silently storing "" would 'succeed' and break the pipeline elsewhere."""
    with pytest.raises(OpError, match="not set"):
        secret.read_value(None, "DEFINITELY_NOT_SET_XYZ", None, False)


def test_from_file_strips_one_trailing_newline(tmp_path):
    p = tmp_path / "secret.txt"
    p.write_text(VALUE + "\n")
    assert secret.read_value(None, None, str(p), False) == VALUE


def test_from_file_strips_only_one_newline(tmp_path):
    p = tmp_path / "secret.txt"
    p.write_text(VALUE + "\n\n")
    assert secret.read_value(None, None, str(p), False) == VALUE + "\n"


def test_from_stdin_strips_the_echo_newline(monkeypatch):
    """`echo hunter2 | ... --from-stdin` must not store a trailing "\\n": the
    resulting auth failure names the *service*, never the secret."""
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(VALUE + "\n"))
    assert secret.read_value(None, None, None, True) == VALUE


def test_from_file_missing_is_a_clean_error(tmp_path):
    with pytest.raises(OpError):
        secret.read_value(None, None, str(tmp_path / "nope"), False)


def test_exactly_one_source_required():
    with pytest.raises(OpError, match="no value"):
        secret.read_value(None, None, None, False)
    with pytest.raises(OpError, match="exactly one"):
        secret.read_value("a", "B", None, False)


def test_empty_value_is_refused():
    """core.Secret.Validate rejects it server-side, and an empty secret reads as
    'set' while being useless."""
    with pytest.raises(ValidationError, match="empty"):
        secret.read_value("", None, None, False)


# ---- name validation --------------------------------------------------


@pytest.mark.parametrize("name", ["docker_password", "a.b-c_1", "X"])
def test_valid_names(name):
    assert secret.validate_name(name) == name


@pytest.mark.parametrize("name", ["has space", "a/b", "", "  ", "a$b", "a:b"])
def test_invalid_names_are_refused_client_side(name):
    with pytest.raises(ValidationError):
        secret.validate_name(name)


# ---- rm ---------------------------------------------------------------


def test_rm_requires_yes_or_a_confirmation():
    client = FakeClient({"t": mk("t")})
    res = runner.invoke(secret.app, ["rm", "t", "-r", SLUG], obj=FakeObj(client), input="n\n")
    assert res.exit_code != 0
    assert not [c for c in client.calls if c[0] == "DELETE"], "aborting must not delete"
    assert "t" in client.store


def test_rm_with_yes_deletes():
    client = FakeClient({"t": mk("t")})
    res = invoke(secret.app, ["rm", "t", "-y", "-r", SLUG], FakeObj(client))
    assert res.exit_code == 0, res.output
    assert client.calls[-1][:2] == ("DELETE", f"repos/{SLUG}/secrets/t")
    assert out_json(res) == {"status": "deleted", "repo": SLUG, "name": "t"}


def test_rm_is_dry_runnable():
    client = FakeClient({"t": mk("t")}, dry_run=True)
    res = invoke(secret.app, ["rm", "t", "-y", "-r", SLUG], FakeObj(client))
    assert isinstance(res.exception, DryRun)
    assert "t" in client.store


# ---- ls ---------------------------------------------------------------


def test_ls_lists_names_and_flags():
    client = FakeClient({"a": mk("a"), "b": mk("b", pull_request=True)})
    res = invoke(secret.app, ["ls", "-r", SLUG], FakeObj(client))
    assert [r["name"] for r in out_json(res)] == ["a", "b"]
    assert client.calls == [("GET", f"repos/{SLUG}/secrets", None)]


def test_ls_of_an_empty_repo_is_an_empty_list_not_an_error():
    res = invoke(secret.app, ["ls", "-r", SLUG], FakeObj(FakeClient()))
    assert out_json(res) == []


def test_repo_comes_from_the_sticky_context():
    obj = FakeObj(FakeClient(), context={"repo": SLUG})
    res = invoke(secret.app, ["ls"], obj)
    assert res.exit_code == 0, res.output
    assert obj._client.calls[0][1] == f"repos/{SLUG}/secrets"


def test_no_repo_anywhere_explains_itself():
    res = invoke(secret.app, ["ls"], FakeObj(FakeClient()))
    assert isinstance(res.exception, ConfigError)
    assert "--repo" in res.exception.message


# ---- org tree ---------------------------------------------------------


def test_org_secrets_use_the_flat_tree_not_orgs_ns_secrets():
    """/api/orgs/{ns}/secrets 404s — verified live, even with a real org. The
    intuitive path is the wrong one, so pin the right one."""
    client = FakeClient({"a": mk("a")})
    invoke(orgsecret.app, ["ls", "--org", "acme"], FakeObj(client))
    invoke(orgsecret.app, ["get", "a", "--org", "acme"], FakeObj(client))
    invoke(orgsecret.app, ["rm", "a", "-y", "--org", "acme"], FakeObj(client))
    paths = [c[1] for c in client.calls]
    assert paths == ["secrets/acme", "secrets/acme/a", "secrets/acme/a"]
    assert not any(p.startswith("orgs/") for p in paths)


def test_org_set_targets_the_namespace():
    client = FakeClient()
    res = invoke(orgsecret.app, ["set", "t", "--value", VALUE, "--org", "acme"], FakeObj(client))
    assert res.exit_code == 0, res.output
    assert client.calls[1][:2] == ("POST", "secrets/acme")


def test_org_defaults_from_the_context_owner():
    obj = FakeObj(FakeClient(), context={"owner": "acme"})
    invoke(orgsecret.app, ["ls"], obj)
    assert obj._client.calls[0][1] == "secrets/acme"


def test_org_falls_back_to_the_namespace_half_of_a_sticky_repo():
    """Someone who set `context set --repo acme/api` has already said 'acme'."""
    obj = FakeObj(FakeClient(), context={"repo": SLUG})
    invoke(orgsecret.app, ["ls"], obj)
    assert obj._client.calls[0][1] == "secrets/droneadmin"


def test_org_rejects_a_repo_slug():
    res = invoke(orgsecret.app, ["ls", "--org", SLUG], FakeObj(FakeClient()))
    assert isinstance(res.exception, ConfigError)
    assert "namespace" in res.exception.message


def test_no_org_anywhere_explains_itself():
    res = invoke(orgsecret.app, ["ls"], FakeObj(FakeClient()))
    assert isinstance(res.exception, ConfigError)
    assert "--org" in res.exception.message


# ---- the reserved-globals rule, locally ------------------------------


def test_no_secret_command_declares_a_reserved_global():
    """The tree-wide test in test_globals_unit.py only covers commands wired into
    cli.py. Until these groups are, assert it here — a `--fields` or `--output` of
    our own could never be received."""
    from typer.main import get_command

    reserved = {"--output", "-o", "--format", "-f", "--fields", "--columns",
                "--dry-run", "--stream", "--no-context"}
    for app_ in (secret.app, orgsecret.app):
        for name, cmd in get_command(app_).commands.items():
            for param in cmd.params:
                assert not (set(param.opts) & reserved), f"{name} declares {param.opts}"
