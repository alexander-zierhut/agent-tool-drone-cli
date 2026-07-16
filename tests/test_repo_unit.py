"""`repo` command behaviour — pure, no server, no network.

The client is a hand-rolled fake that records calls and replays canned bodies:
these tests are about what the CLI *decides*, not about HTTP. Fixtures are
shaped from real `drone/drone:2` responses captured during the 2026-07-16 spike
— in particular they honour `omitempty`, so `active: false` and `timeout: 0`
are ABSENT from a repo object rather than present-and-falsy. Several of the
guards below exist only because of that.
"""

from __future__ import annotations

import types

import pytest
import typer
from agentcli.errors import ApiError, ConfigError, OpError, ValidationError

from dronecli.commands import repo as R


# ---- fakes ------------------------------------------------------------

class FakeEmitter:
    def __init__(self):
        self.data = None
        self.columns = None
        self.messages = []

    def emit(self, data, *, columns=None, **kw):
        self.data = data
        self.columns = list(columns) if columns else None

    def message(self, text):
        self.messages.append(text)


class FakeClient:
    """Records every call; replays whatever the test queued.

    `pages` mimics the real `GET /api/user/repos`, which ignores page/per_page
    and hands back the full array on every request.
    """

    def __init__(self, *, repos=None, responses=None):
        self.repos = repos or []
        self.responses = responses or {}
        self.calls = []

    def paginate(self, path, *, params=None, limit=0):
        while True:  # the endpoint has no paging: every page is the whole list
            self.calls.append(("GET", path, params))
            yield from self.repos

    def _reply(self, method, path, params=None, json=None):
        self.calls.append((method, path, params or json))
        val = self.responses.get((method, path), self.responses.get(path))
        if isinstance(val, Exception):
            raise val
        if callable(val):
            return val(json)
        return val

    def get(self, path, **kw):
        return self._reply("GET", path, params=kw.get("params"))

    def post(self, path, **kw):
        return self._reply("POST", path, params=kw.get("params"), json=kw.get("json"))

    def patch(self, path, **kw):
        return self._reply("PATCH", path, json=kw.get("json"))

    def delete(self, path, **kw):
        return self._reply("DELETE", path)


def mkctx(client, *, context=None, interactive=False):
    obj = types.SimpleNamespace(
        emitter=FakeEmitter(),
        config=types.SimpleNamespace(context=context or {}),
        client=lambda: client,
        # Non-interactive by default: that is what an agent, a script and CI all
        # look like, so a prompt reached from a test is a bug the test must feel.
        interactive=interactive,
    )
    return types.SimpleNamespace(obj=obj), obj


def mkrepo(slug, *, active=True, branch="main", **kw):
    ns, name = slug.split("/")
    out = {"id": abs(hash(slug)) % 1000, "namespace": ns, "name": name, "slug": slug,
           "default_branch": branch, "visibility": "private",
           "link": f"http://gitea:3000/{slug}", **kw}
    if active:  # omitempty: an inactive repo has NO `active` key at all
        out["active"] = True
    return out


def call(fn, ctx, **kw):
    """Invoke a command function directly, filling in its declared defaults."""
    import inspect
    sig = inspect.signature(fn)
    args = {}
    for name, p in sig.parameters.items():
        if name == "ctx":
            continue
        default = p.default
        args[name] = kw.get(name, getattr(default, "default", None))
    return fn(ctx=ctx, **args)


# ---- ls ---------------------------------------------------------------

def test_ls_shows_only_active_by_default():
    """"Which repos do I have" almost always means "which does Drone build".
    A synced-but-never-enabled repo is noise on any real org."""
    client = FakeClient(repos=[mkrepo("acme/api"), mkrepo("acme/old", active=False)])
    ctx, obj = mkctx(client)
    call(R.ls, ctx)
    assert [r["slug"] for r in obj.emitter.data] == ["acme/api"]


def test_ls_all_includes_inactive():
    client = FakeClient(repos=[mkrepo("acme/api"), mkrepo("acme/old", active=False)])
    ctx, obj = mkctx(client)
    call(R.ls, ctx, all_repos=True)
    assert [r["slug"] for r in obj.emitter.data] == ["acme/api", "acme/old"]


def test_ls_terminates_when_the_endpoint_ignores_paging():
    """`GET /api/user/repos` returns the FULL array and ignores page/per_page,
    so the generic short-page terminator never fires past 100 repos: page 2 is
    page 1 again. Without the seen-slug guard this hangs forever and, worse,
    emits every repo N times."""
    client = FakeClient(repos=[mkrepo(f"acme/r{i}") for i in range(120)])
    ctx, obj = mkctx(client)
    call(R.ls, ctx)
    slugs = [r["slug"] for r in obj.emitter.data]
    assert len(slugs) == len(set(slugs)) == 120
    assert len(client.calls) <= 3, "must not keep re-requesting the same array"


def test_ls_latest_requests_the_undocumented_flag_and_surfaces_status():
    client = FakeClient(repos=[mkrepo("acme/api", build={"number": 7, "status": "failure"})])
    ctx, obj = mkctx(client)
    call(R.ls, ctx, latest=True)
    assert client.calls[0][2] == {"latest": "true"}
    row = obj.emitter.data[0]
    assert row["last_build_status"] == "failure" and row["last_build_number"] == 7
    assert "last_build_status" in obj.emitter.columns


def test_ls_without_latest_does_not_ask_for_it():
    """`latest=true` is extra server work per repo — don't pay for it unasked."""
    client = FakeClient(repos=[mkrepo("acme/api")])
    ctx, obj = mkctx(client)
    call(R.ls, ctx)
    assert client.calls[0][2] is None
    assert "last_build_status" not in (obj.emitter.columns or [])


def test_ls_of_a_repo_that_never_built_has_no_status():
    """No build != a failed build. `build` is absent, not null, and must not
    materialise as a status of None in a fleet-health table."""
    client = FakeClient(repos=[mkrepo("acme/fresh")])
    ctx, obj = mkctx(client)
    call(R.ls, ctx, latest=True)
    assert "last_build_status" not in obj.emitter.data[0]


def test_ls_links_adds_the_web_url():
    client = FakeClient(repos=[mkrepo("acme/api")])
    ctx, obj = mkctx(client)
    call(R.ls, ctx, links=True)
    assert obj.emitter.data[0]["repo_url"] == "http://gitea:3000/acme/api"
    assert "repo_url" in obj.emitter.columns


def test_ls_filters_by_namespace_and_search():
    client = FakeClient(repos=[mkrepo("acme/api"), mkrepo("acme/web"), mkrepo("other/api")])
    ctx, obj = mkctx(client)
    call(R.ls, ctx, namespace="acme")
    assert [r["slug"] for r in obj.emitter.data] == ["acme/api", "acme/web"]
    call(R.ls, ctx, search="WE")
    assert [r["slug"] for r in obj.emitter.data] == ["acme/web"], "search is case-insensitive"


# ---- sync -------------------------------------------------------------

def test_sync_posts_and_returns_the_refreshed_list():
    """POST is the sync; the refreshed list IS the body. Nothing to poll."""
    client = FakeClient(responses={("POST", "user/repos"): [mkrepo("acme/api"), mkrepo("acme/new")]})
    ctx, obj = mkctx(client)
    call(R.sync, ctx)
    assert client.calls == [("POST", "user/repos", None)]
    assert [r["slug"] for r in obj.emitter.data] == ["acme/api", "acme/new"]


def test_sync_survives_a_bodyless_response():
    """A 204 with no body must emit an empty list, not crash on None."""
    client = FakeClient(responses={("POST", "user/repos"): None})
    ctx, obj = mkctx(client)
    call(R.sync, ctx)
    assert obj.emitter.data == []


# ---- info -------------------------------------------------------------

def test_info_takes_the_slug_positionally():
    client = FakeClient(responses={("GET", "repos/acme/api"): mkrepo("acme/api")})
    ctx, obj = mkctx(client)
    call(R.info, ctx, slug_arg="acme/api")
    assert obj.emitter.data["slug"] == "acme/api"


def test_info_falls_back_to_the_sticky_context():
    client = FakeClient(responses={("GET", "repos/acme/api"): mkrepo("acme/api")})
    ctx, obj = mkctx(client, context={"repo": "acme/api"})
    call(R.info, ctx)
    assert obj.emitter.data["slug"] == "acme/api"


def test_info_without_a_repo_anywhere_explains_itself():
    ctx, _ = mkctx(FakeClient())
    with pytest.raises(ConfigError):
        call(R.info, ctx)


def test_info_links():
    client = FakeClient(responses={("GET", "repos/acme/api"): mkrepo("acme/api")})
    ctx, obj = mkctx(client)
    call(R.info, ctx, slug_arg="acme/api", links=True)
    assert obj.emitter.data["repo_url"] == "http://gitea:3000/acme/api"


# ---- enable / disable -------------------------------------------------

def test_enable_activates_and_warns_about_the_implicit_chown():
    """Enabling silently sets repo.UserID = caller, moving every webhook, clone
    and config fetch onto the caller's SCM token. Nothing in the API says so."""
    client = FakeClient(responses={("POST", "repos/acme/api"): mkrepo("acme/api")})
    ctx, obj = mkctx(client)
    call(R.enable, ctx, slug_arg="acme/api")
    assert client.calls == [("POST", "repos/acme/api", None)]
    assert "OWNED BY YOU" in obj.emitter.data["note"]


def test_enable_sync_first_syncs_before_activating():
    """A repo created two minutes ago is not in Drone yet; activating it 404s
    until a sync pulls it from the provider."""
    client = FakeClient(responses={
        ("POST", "user/repos"): [mkrepo("acme/api")],
        ("POST", "repos/acme/api"): mkrepo("acme/api"),
    })
    ctx, _ = mkctx(client)
    call(R.enable, ctx, slug_arg="acme/api", sync_first=True)
    assert [c[1] for c in client.calls] == ["user/repos", "repos/acme/api"]


def test_enable_402_is_translated_into_the_repo_limit():
    """402 on a licensed server means "no free repo slots", not "your request
    was wrong" and certainly not "pay us". Left bare it sends people debugging
    the payload."""
    client = FakeClient(responses={
        ("POST", "repos/acme/api"): ApiError("Payment Required", status=402),
    })
    ctx, _ = mkctx(client)
    with pytest.raises(ApiError) as exc:
        call(R.enable, ctx, slug_arg="acme/api")
    assert exc.value.status == 402
    assert "limit" in str(exc.value).lower()


def test_enable_does_not_swallow_other_api_errors():
    client = FakeClient(responses={("POST", "repos/acme/api"): ApiError("boom", status=503)})
    ctx, _ = mkctx(client)
    with pytest.raises(ApiError) as exc:
        call(R.enable, ctx, slug_arg="acme/api")
    assert exc.value.status == 503


def test_disable_deletes_and_says_the_webhook_survives():
    """Correction to the docs and to common belief: DELETE does not unregister
    the hook — the provider keeps delivering and Drone keeps ignoring."""
    client = FakeClient(responses={("DELETE", "repos/acme/api"): None})
    ctx, obj = mkctx(client)
    call(R.disable, ctx, slug_arg="acme/api")
    assert client.calls == [("DELETE", "repos/acme/api", None)]
    assert obj.emitter.data["active"] is False
    assert "webhook" in obj.emitter.data["note"].lower()


# ---- update: the silent-drop guard ------------------------------------

def test_update_sends_only_the_flags_given():
    client = FakeClient(responses={("PATCH", "repos/acme/api"): mkrepo("acme/api", timeout=90)})
    ctx, obj = mkctx(client)
    call(R.update, ctx, slug_arg="acme/api", timeout=90)
    assert client.calls[0][2] == {"timeout": 90}, "an unset flag must never be sent"
    assert obj.emitter.data["applied"] == ["timeout"]


def test_update_fails_loudly_when_the_server_drops_a_field():
    """THE bug this command exists for: `timeout` is gated on SYSTEM admin and
    is dropped in SILENCE for everyone else — HTTP 200, old value, no warning.
    A thin PATCH client reports success and the agent believes the timeout
    changed. It did not."""
    client = FakeClient(responses={
        ("PATCH", "repos/acme/api"): mkrepo("acme/api", timeout=60),  # asked 90, got 60
        ("GET", "user"): {"login": "me", "admin": False},
    })
    ctx, _ = mkctx(client)
    with pytest.raises(ValidationError) as exc:
        call(R.update, ctx, slug_arg="acme/api", timeout=90)
    msg = str(exc.value)
    assert "--timeout" in msg, "the dropped field must be named"
    assert "system admin" in msg.lower(), "and the reason given"
    assert exc.value.detail["dropped"] == ["timeout"]
    assert exc.value.detail["effective"] == {"timeout": 60}


def test_update_drop_is_reported_even_when_admin_lookup_fails():
    """The nicety (naming your admin status) must never suppress the finding."""
    client = FakeClient(responses={
        ("PATCH", "repos/acme/api"): mkrepo("acme/api", timeout=60),
        ("GET", "user"): ApiError("nope", status=500),
    })
    ctx, _ = mkctx(client)
    with pytest.raises(ValidationError):
        call(R.update, ctx, slug_arg="acme/api", timeout=90)


def test_update_names_every_dropped_field_not_just_the_first():
    client = FakeClient(responses={
        ("PATCH", "repos/acme/api"): mkrepo("acme/api", timeout=60, protected=True),
        ("GET", "user"): {"admin": False},
    })
    ctx, _ = mkctx(client)
    with pytest.raises(ValidationError) as exc:
        call(R.update, ctx, slug_arg="acme/api", timeout=90, trusted=True, trusted_ack=True)
    assert exc.value.detail["dropped"] == ["timeout", "trusted"]


def test_update_applying_false_is_not_a_drop():
    """omitempty: an applied `protected: false` comes back ABSENT, not false. A
    naive `!=` diff would cry wolf on every --no-protected — which would make
    the guard worthless precisely where it matters."""
    client = FakeClient(responses={("PATCH", "repos/acme/api"): mkrepo("acme/api")})
    ctx, obj = mkctx(client)
    call(R.update, ctx, slug_arg="acme/api", protected=False)
    assert obj.emitter.data["applied"] == ["protected"]


def test_update_detects_a_dropped_false():
    """The other direction: we asked for false, the object still says true."""
    client = FakeClient(responses={
        ("PATCH", "repos/acme/api"): mkrepo("acme/api", trusted=True),
        ("GET", "user"): {"admin": False},
    })
    ctx, _ = mkctx(client)
    with pytest.raises(ValidationError) as exc:
        call(R.update, ctx, slug_arg="acme/api", trusted=False)
    assert exc.value.detail["dropped"] == ["trusted"]


def test_update_zero_timeout_round_trips():
    client = FakeClient(responses={("PATCH", "repos/acme/api"): mkrepo("acme/api")})
    ctx, obj = mkctx(client)
    call(R.update, ctx, slug_arg="acme/api", timeout=0)
    assert obj.emitter.data["applied"] == ["timeout"]


def test_update_config_path_and_visibility_round_trip():
    client = FakeClient(responses={
        ("PATCH", "repos/acme/api"): mkrepo("acme/api", visibility="public", config_path=".drone.yml"),
    })
    ctx, obj = mkctx(client)
    call(R.update, ctx, slug_arg="acme/api", visibility="public", config_path=".drone.yml")
    assert client.calls[0][2] == {"visibility": "public", "config_path": ".drone.yml"}
    assert obj.emitter.data["applied"] == ["config_path", "visibility"]


@pytest.mark.parametrize("bad", ["pubic", "PUBLIC", "internal ", ""])
def test_update_rejects_a_bad_visibility_client_side(bad):
    """The server's own IsIn check is commented out: it would accept `pubic`,
    store it and answer 200 forever. Here is the only place it can be caught."""
    ctx, _ = mkctx(FakeClient())
    with pytest.raises(ValidationError):
        call(R.update, ctx, slug_arg="acme/api", visibility=bad)


def test_update_with_no_flags_is_a_usage_error_not_an_empty_patch():
    ctx, _ = mkctx(FakeClient())
    with pytest.raises(ValidationError) as exc:
        call(R.update, ctx, slug_arg="acme/api")
    assert "--timeout" in str(exc.value), "say what could be updated"


# ---- update: the --trusted privilege gate -----------------------------
#
# `trusted` grants privileged containers and host mounts to every pipeline in the
# repo — root on the runner for anyone who can push a .drone.yml. It is a
# privilege escalation wearing the same clothes as `--config-path`. The rule
# defended below: it is NEVER granted silently.


def test_granting_trusted_off_a_tty_refuses_without_the_ack_flag():
    """An agent, a script and CI all look like this. Refusing is the whole point:
    there is nobody to prompt, so the only honest options are "say it explicitly"
    or "don't"."""
    client = FakeClient(responses={("PATCH", "repos/acme/api"): mkrepo("acme/api", trusted=True)})
    ctx, _ = mkctx(client)
    with pytest.raises(OpError) as exc:
        call(R.update, ctx, slug_arg="acme/api", trusted=True)
    msg = str(exc.value)
    assert R.TRUSTED_ACK_FLAG in msg, "refusing must name the way to proceed"
    assert "privileged" in msg.lower(), "and say what is being granted"
    assert client.calls == [], "REFUSED means nothing was sent — not sent-then-regretted"


def test_the_ack_flag_grants_it_without_a_prompt():
    client = FakeClient(responses={("PATCH", "repos/acme/api"): mkrepo("acme/api", trusted=True)})
    ctx, obj = mkctx(client)
    call(R.update, ctx, slug_arg="acme/api", trusted=True, trusted_ack=True)
    assert client.calls[0][2] == {"trusted": True}
    assert obj.emitter.data["applied"] == ["trusted"], "the ack is consent, not a payload field"


def test_granting_trusted_on_a_tty_confirms_first(monkeypatch):
    asked = []

    def fake_confirm(text, abort=False, err=False):
        asked.append(text)
        raise typer.Abort()

    monkeypatch.setattr(R.typer, "confirm", fake_confirm)
    client = FakeClient(responses={("PATCH", "repos/acme/api"): mkrepo("acme/api", trusted=True)})
    ctx, _ = mkctx(client, interactive=True)
    with pytest.raises(typer.Abort):
        call(R.update, ctx, slug_arg="acme/api", trusted=True)
    assert "privileged" in asked[0].lower() and "acme/api" in asked[0]
    assert client.calls == [], "aborting must not have granted anything"


def test_the_confirm_prompt_goes_to_stderr(monkeypatch):
    """stdout is the machine channel; a question in it corrupts the payload."""
    seen = {}

    def fake_confirm(text, abort=False, err=False):
        seen["err"] = err
        return True

    monkeypatch.setattr(R.typer, "confirm", fake_confirm)
    client = FakeClient(responses={("PATCH", "repos/acme/api"): mkrepo("acme/api", trusted=True)})
    ctx, _ = mkctx(client, interactive=True)
    call(R.update, ctx, slug_arg="acme/api", trusted=True)
    assert seen["err"] is True


def test_accepting_the_prompt_proceeds(monkeypatch):
    monkeypatch.setattr(R.typer, "confirm", lambda *a, **kw: True)
    client = FakeClient(responses={("PATCH", "repos/acme/api"): mkrepo("acme/api", trusted=True)})
    ctx, obj = mkctx(client, interactive=True)
    call(R.update, ctx, slug_arg="acme/api", trusted=True)
    assert client.calls[0][2] == {"trusted": True}
    assert obj.emitter.data["applied"] == ["trusted"]


def test_revoking_trusted_needs_no_gate(monkeypatch):
    """--no-trusted takes privilege AWAY. Gating it would train people to reach
    for the ack flag reflexively, which is how a guard stops guarding."""
    def boom(*a, **kw):
        raise AssertionError("--no-trusted must never prompt")

    monkeypatch.setattr(R.typer, "confirm", boom)
    client = FakeClient(responses={("PATCH", "repos/acme/api"): mkrepo("acme/api")})
    ctx, obj = mkctx(client)  # not interactive, no ack flag
    call(R.update, ctx, slug_arg="acme/api", trusted=False)
    assert client.calls[0][2] == {"trusted": False}
    assert obj.emitter.data["applied"] == ["trusted"]


def test_an_update_that_never_mentions_trusted_is_never_gated(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("only --trusted is gated")

    monkeypatch.setattr(R.typer, "confirm", boom)
    client = FakeClient(responses={("PATCH", "repos/acme/api"): mkrepo("acme/api", timeout=90)})
    ctx, _ = mkctx(client)
    call(R.update, ctx, slug_arg="acme/api", timeout=90)
    assert client.calls[0][2] == {"timeout": 90}


def test_the_ack_flag_alone_is_not_an_update():
    """It is consent, not an instruction — and it must not sneak into the body."""
    ctx, _ = mkctx(FakeClient())
    with pytest.raises(ValidationError) as exc:
        call(R.update, ctx, slug_arg="acme/api", trusted_ack=True)
    assert "nothing to update" in str(exc.value)


def test_a_consented_trusted_that_the_server_drops_is_still_a_failure():
    """Consent is not permission. `trusted` is admin-gated server-side and
    dropped in SILENCE for everyone else — the gate must not be mistaken for
    proof it landed."""
    client = FakeClient(responses={
        ("PATCH", "repos/acme/api"): mkrepo("acme/api"),  # asked trusted, got nothing
        ("GET", "user"): {"admin": False},
    })
    ctx, _ = mkctx(client)
    with pytest.raises(ValidationError) as exc:
        call(R.update, ctx, slug_arg="acme/api", trusted=True, trusted_ack=True)
    assert exc.value.detail["dropped"] == ["trusted"]
    assert "system admin" in str(exc.value).lower()


# ---- repair / chown ---------------------------------------------------

def test_repair_rereads_the_repo_and_warns_the_hook_is_appended():
    """The endpoint returns nothing observable, so a printed "success" would
    prove nothing — re-read instead. And repair APPENDS a hook: repairing a bad
    URL three times leaves three hooks, two of them dead."""
    client = FakeClient(responses={
        ("POST", "repos/acme/api/repair"): None,
        ("GET", "repos/acme/api"): mkrepo("acme/api"),
    })
    ctx, obj = mkctx(client)
    call(R.repair, ctx, slug_arg="acme/api")
    assert [c[1] for c in client.calls] == ["repos/acme/api/repair", "repos/acme/api"]
    assert "APPENDED" in obj.emitter.data["note"]
    assert obj.emitter.data["slug"] == "acme/api"


def test_chown_takes_ownership_and_points_at_repair():
    client = FakeClient(responses={("POST", "repos/acme/api/chown"): mkrepo("acme/api")})
    ctx, obj = mkctx(client)
    call(R.chown, ctx, slug_arg="acme/api")
    assert client.calls[0][1] == "repos/acme/api/chown"
    assert "repo repair" in obj.emitter.data["note"]


def test_chown_rereads_when_the_post_returns_nothing():
    client = FakeClient(responses={
        ("POST", "repos/acme/api/chown"): None,
        ("GET", "repos/acme/api"): mkrepo("acme/api"),
    })
    ctx, obj = mkctx(client)
    call(R.chown, ctx, slug_arg="acme/api")
    assert obj.emitter.data["slug"] == "acme/api"


# ---- the reserved-globals rule ---------------------------------------

def test_no_command_declares_a_reserved_global():
    """--output/-o, --format/-f, --fields/--columns, --dry-run, --stream and
    --no-context are stripped from argv before Click ever sees them, so a
    command declaring one could never receive it. It would silently take its
    default — which shipped as a wrong-file write in the sibling CLI."""
    reserved = {"--output", "-o", "--format", "-f", "--fields", "--columns",
                "--dry-run", "--stream", "--no-context"}
    for cmd in R.app.registered_commands:
        for name, param in __import__("inspect").signature(cmd.callback).parameters.items():
            decls = set(getattr(param.default, "param_decls", None) or [])
            assert not (decls & reserved), f"{cmd.callback.__name__} declares a reserved global"
