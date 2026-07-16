"""`server` + `user` — the diagnosis and admin surface. Pure, no server, no sockets.

The shapes below are the ones observed live on 2026-07-16 against
drone/drone:2 + gitea 1.22 (see spike/VERIFIED_FINDINGS.md).

What is really under test here is *naming*: Drone renders "the URL is wrong",
"your token is wrong", "you are not an admin" and "the SERVER's git login is
dead" as the same 401/500 "Unauthorized". Every assertion that pins a
`diagnosis` string is pinning the one thing this module exists to provide.
"""

from __future__ import annotations

import httpx
import pytest
import typer
from agentcli.errors import ApiError, AuthError, ConfigError, NotFoundError, OpError, ValidationError

from dronecli.client import Client
from dronecli.commands import server as S
from dronecli.commands import user as U
from dronecli.errors import NotImplementedOnServer

WEB = "http://drone.example.com"


# ---- doubles ----------------------------------------------------------

class FakeEmitter:
    """Captures what a command emits. Only `emit` is a payload channel."""

    def __init__(self):
        self.emitted = None
        self.columns = None
        self.messages: list[str] = []

    def emit(self, data, *, columns=None, **kw):
        self.emitted = data
        self.columns = columns

    def message(self, text):
        self.messages.append(text)


class FakeClient:
    """A client-shaped object. `routes` maps a path to a value or an exception
    instance to raise — the two things a real client can do to a caller."""

    def __init__(self, *, routes=None, version=None, web_root=WEB):
        self.web_root = web_root
        self.api_root = web_root + "/api"
        self.routes = routes or {}
        self._version = version
        self.calls: list[tuple[str, str]] = []

    def _answer(self, method, path):
        self.calls.append((method, path))
        if path not in self.routes:
            raise NotFoundError("404 page not found")
        val = self.routes[path]
        if isinstance(val, Exception):
            raise val
        return val

    def version(self):
        self.calls.append(("GET", "/version"))
        if isinstance(self._version, Exception):
            raise self._version
        return self._version

    def get(self, path, **kw):
        return self._answer("GET", path)

    def post(self, path, **kw):
        self.body = kw.get("json")
        return self._answer("POST", path)

    def patch(self, path, **kw):
        self.body = kw.get("json")
        return self._answer("PATCH", path)

    def delete(self, path, **kw):
        return self._answer("DELETE", path)


class FakeConfig:
    def __init__(self):
        self.context = {}

    def active_profile_name(self):
        return "default"


class FakeObj:
    def __init__(self, client):
        self.config = FakeConfig()
        self.emitter = FakeEmitter()
        self._client = client

    def client(self):
        return self._client


class FakeCtx:
    def __init__(self, obj):
        self.obj = obj


def mk(client=None, **routes):
    obj = FakeObj(client or FakeClient(routes=routes))
    return FakeCtx(obj), obj


@pytest.fixture(autouse=True)
def _quiet_credentials(monkeypatch):
    """Never touch the real keyring (or the developer's real DRONE_TOKEN)."""
    monkeypatch.setattr(S.credentials, "backend_name", lambda: "OS keyring (TestKeyring)")
    monkeypatch.delenv("DRONE_TOKEN", raising=False)
    monkeypatch.delenv("DRONECLI_TOKEN", raising=False)


def _diag(report, check):
    return next(c for c in report["checks"] if c["check"] == check)


# ---- the seam with client.py -----------------------------------------

def test_the_500_unauthorized_mapping_is_recognised_as_the_scm_case():
    """The highest-value assertion in this file.

    Drone reports "the SERVER's git credentials are dead" as HTTP 500 with body
    {"message":"Unauthorized"} (verified live: POST /api/user/repos with no SCM
    token). client.py owns that mapping; doctor has to read it back to name the
    diagnosis. If someone rewords client.py's message, doctor silently starts
    calling the most confusing failure in the system "bad-token" instead — this
    test is the tripwire.
    """
    resp = httpx.Response(500, json={"message": "Unauthorized"}, request=httpx.Request("POST", WEB))
    with pytest.raises(AuthError) as caught:
        Client._raise_for_error(resp)
    assert S.is_scm_link_error(caught.value)

    plain = httpx.Response(401, json={"message": "Unauthorized"}, request=httpx.Request("GET", WEB))
    with pytest.raises(AuthError) as caught401:
        Client._raise_for_error(plain)
    assert not S.is_scm_link_error(caught401.value), "a real 401 must not be blamed on the SCM"


# ---- probe 1: reachable / is this even Drone --------------------------

def test_version_probe_ok():
    c = FakeClient(version={"source": "github.com/harness/drone", "version": "2.25.0", "commit": "abc"})
    got = S.probe_version(c)
    assert got["ok"] and got["diagnosis"] is None
    assert got["version"] == "2.25.0"


def test_plain_text_404_is_named_not_a_drone_server():
    """A wrong host/port answers with text, and client.py hands the text through
    rather than exploding. "not JSON" is a diagnosis, not a parse bug."""
    c = FakeClient(version="404 page not found\n")
    got = S.probe_version(c)
    assert not got["ok"]
    assert got["diagnosis"] == "not-a-drone-server"


def test_404_on_version_is_not_a_drone_server_not_unreachable():
    c = FakeClient(version=NotFoundError("404 page not found"))
    got = S.probe_version(c)
    assert got["diagnosis"] == "not-a-drone-server"


def test_connect_failure_is_unreachable():
    c = FakeClient(version=ApiError(f"cannot reach {WEB}: [Errno -2] Name or service not known"))
    got = S.probe_version(c)
    assert got["diagnosis"] == "unreachable"


def test_auth_on_version_means_something_is_in_front_of_drone():
    """/version is unauthenticated on every Drone. A 401 there is a proxy."""
    c = FakeClient(version=AuthError("Unauthorized"))
    assert S.probe_version(c)["diagnosis"] == "not-a-drone-server"


# ---- probe 2: the token ----------------------------------------------

ME = {"id": 1, "login": "droneadmin", "email": "a@b.c", "machine": False, "admin": True, "active": True}


def test_token_probe_ok_admin():
    got = S.probe_token(FakeClient(routes={"user": ME}), has_token=True)
    assert got["ok"] and got["admin"] is True
    assert "warning" not in got


def test_no_token_is_its_own_diagnosis():
    """Distinct from a bad token: nothing was even attempted. And on a default
    server public repos read 200 with no token, so this hides for a long time."""
    got = S.probe_token(FakeClient(routes={"user": ME}), has_token=False)
    assert got["diagnosis"] == "no-token"


def test_bad_token_is_named_bad_token():
    c = FakeClient(routes={"user": AuthError("Unauthorized")})
    got = S.probe_token(c, has_token=True)
    assert got["diagnosis"] == "bad-token"
    assert "/account" in got["message"], "tell them where the real token lives"


def test_scm_500_on_the_token_probe_is_not_blamed_on_the_token():
    """The single most confusing failure in Drone: your token is FINE."""
    c = FakeClient(routes={"user": AuthError(
        "the server rejected its own SCM credentials (HTTP 500 'Unauthorized'). ..."
    )})
    got = S.probe_token(c, has_token=True)
    assert got["diagnosis"] == "scm-link-broken"


def test_non_admin_is_a_warning_not_a_failure():
    """Not being an admin is a fact about you, not a broken server."""
    c = FakeClient(routes={"user": {**ME, "admin": False}})
    got = S.probe_token(c, has_token=True)
    assert got["ok"] is True
    assert got["admin"] is False
    assert "admin" in got["warning"]


def test_a_login_less_answer_is_not_drone():
    """Some other JSON API answering /api/user 200 must not read as 'logged in'."""
    c = FakeClient(routes={"user": {"hello": "world"}})
    assert S.probe_token(c, has_token=True)["diagnosis"] == "not-a-drone-server"


def test_a_404_on_the_token_probe_is_not_blamed_on_the_token():
    """Every Drone serves GET /api/user. A 404 means there is no Drone here (or a
    proxy is eating /api) — telling the caller their token is bad would send them
    to rotate a credential that was never involved."""
    c = FakeClient(routes={"user": NotFoundError("404 page not found")})
    got = S.probe_token(c, has_token=True)
    assert got["diagnosis"] == "not-a-drone-server"
    assert "bad-token" not in got["message"]


def test_an_unclassifiable_token_failure_does_not_claim_the_token_is_bad():
    """A 400/501 from /api/user proves nothing about the token either way, and
    `bad-token` is an expensive wrong answer: it costs a rotation."""
    c = FakeClient(routes={"user": ValidationError("invalid request")})
    got = S.probe_token(c, has_token=True)
    assert got["diagnosis"] == "probe-failed"
    assert "unverified" in got["message"]


# ---- probe 3: /varz ---------------------------------------------------

VARZ = {
    "scm": {"url": "http://gitea:3000", "rate": {"limit": 5000, "remaining": 4980, "reset": 1752700000}},
    "license": {"kind": "trial", "seats": 10, "seats_used": 3},
}


def _varz_client(payload):
    return FakeClient(routes={f"{WEB}/varz": payload})


def test_varz_ok():
    got = S.probe_varz(_varz_client(VARZ))
    assert got["ok"]
    assert got["scm_rate_remaining"] == 4980
    assert got["license_seats"] == 10


def test_varz_403_degrades_and_never_kills_the_doctor():
    """/varz may be admin-gated. A missing bonus is not a failed diagnosis."""
    got = S.probe_varz(_varz_client(AuthError("Unauthorized")))
    assert got["diagnosis"] == "not-admin"
    assert got["ok"] is False  # reported, but see test_doctor_degrades_on_varz


def test_exhausted_scm_quota_is_named():
    payload = {**VARZ, "scm": {"url": "x", "rate": {"limit": 5000, "remaining": 0, "reset": 1752700000}}}
    got = S.probe_varz(_varz_client(payload))
    assert got["diagnosis"] == "scm-quota-exhausted"
    assert "500" in got["message"], "say how it will manifest, or nobody connects the two"


def test_nearly_exhausted_quota_warns_but_stays_ok():
    payload = {**VARZ, "scm": {"rate": {"limit": 5000, "remaining": 10}}}
    got = S.probe_varz(_varz_client(payload))
    assert got["ok"] and got["diagnosis"] is None
    assert "10 of 5000" in got["warning"]


def test_full_license_seats_warns_about_the_402():
    payload = {**VARZ, "license": {"kind": "trial", "seats": 10, "seats_used": 10}}
    got = S.probe_varz(_varz_client(payload))
    assert "402" in got["warning"]


def test_varz_missing_rate_block_does_not_explode():
    """Every field on /varz is omitempty — absence is normal, not an error."""
    got = S.probe_varz(_varz_client({}))
    assert got["ok"] and got["scm_rate_limit"] is None


def test_varz_404_is_reported_not_raised():
    """/varz lives on the WEB root, so a proxy forwarding only /api 404s it.

    `NotFoundError` and `ApiError` are SIBLINGS in agentcli.errors — neither
    catches the other — so `except ApiError` let this one escape and take the
    whole doctor with it. The bonus probe must never be the reason the operator
    gets no report.
    """
    got = S.probe_varz(_varz_client(NotFoundError("404 page not found")))
    assert got["check"] == "capacity" and got["ok"] is False
    assert got["diagnosis"] is None, "an unserved bonus earns no diagnosis of its own"
    assert "404" in got["message"]


def test_the_sibling_trap_this_file_guards_is_real():
    """Pin the assumption every probe's except-ladder is built on.

    If NotFoundError ever became an ApiError subclass, the ladders below would
    still work — but the reverse change (or a new sibling) is what breaks them,
    and this is the tripwire that says so out loud.
    """
    assert not issubclass(NotFoundError, ApiError), "404s do not ride the ApiError branch"
    assert issubclass(NotFoundError, OpError) and issubclass(ApiError, OpError)


@pytest.mark.parametrize(
    "boom",
    [
        NotFoundError("404 page not found"),
        ValidationError("invalid request"),
        NotImplementedOnServer("not implemented"),
        AuthError("Unauthorized"),
        ApiError("boom", status=503),
    ],
)
def test_no_probe_raises_whatever_the_client_throws(boom):
    """Every error the client is capable of raising, against every probe.

    Parametrised rather than spelled out per-probe because the leak was never
    about one error — it was about an except-ladder that looked exhaustive and
    wasn't. This asserts the property directly: a probe RETURNS.
    """
    assert S.probe_version(FakeClient(version=boom))["ok"] is False
    assert S.probe_token(FakeClient(routes={"user": boom}), has_token=True)["ok"] is False
    assert S.probe_varz(_varz_client(boom))["ok"] is False


def test_a_probe_that_raises_something_unforeseen_becomes_a_row(monkeypatch):
    """The floor under the ladders: not every future bug is an OpError."""
    got = S._run_probe("capacity", lambda: 1 / 0)
    assert got == {
        "check": "capacity", "ok": False, "diagnosis": "probe-failed",
        "message": got["message"],
    }
    assert "ZeroDivisionError" in got["message"]
    assert "gap in this CLI" in got["message"], "do not blame the server for our bug"


# ---- doctor: the report ----------------------------------------------

def _doctor(monkeypatch, client, token="tok"):
    ctx, obj = mk(client)
    monkeypatch.setattr(S, "_open_client", lambda o: (client, token))
    S.doctor(ctx)
    return obj.emitter.emitted


def test_doctor_healthy(monkeypatch):
    c = FakeClient(version={"version": "2.25.0"}, routes={"user": ME, f"{WEB}/varz": VARZ})
    rep = _doctor(monkeypatch, c)
    assert rep["status"] == "ok"
    assert rep["problems"] == [] and rep["warnings"] == []
    assert [x["check"] for x in rep["checks"]] == ["reachable", "token", "capacity"]


def test_doctor_reports_which_credential_is_speaking(monkeypatch):
    """An exported DRONE_TOKEN silently beats a keyring login — and DRONE_* is the
    namespace the runner injects into every build step, so a pipeline can
    authenticate as a stranger. Doctor must say so unprompted."""
    monkeypatch.setenv("DRONE_TOKEN", "from-the-environment")
    c = FakeClient(version={"version": "2.25.0"}, routes={"user": ME, f"{WEB}/varz": VARZ})
    rep = _doctor(monkeypatch, c)
    assert "$DRONE_TOKEN" in rep["credential"]["note"]
    assert "keyring" in rep["credential"]["note"]


def test_doctor_says_nothing_about_env_when_the_keyring_is_speaking(monkeypatch):
    c = FakeClient(version={"version": "2.25.0"}, routes={"user": ME, f"{WEB}/varz": VARZ})
    rep = _doctor(monkeypatch, c)
    assert "note" not in rep["credential"]
    assert rep["credential"]["backend"] == "OS keyring (TestKeyring)"


def test_doctor_never_raises_when_the_server_is_down(monkeypatch):
    """Its entire job is to return a report. Raising here would leave the operator
    exactly where they started: with an exception and no diagnosis."""
    c = FakeClient(version=ApiError(f"cannot reach {WEB}: connection refused"))
    rep = _doctor(monkeypatch, c, token=None)
    assert rep["status"] == "failed"
    assert "unreachable" in rep["problems"]


def test_doctor_does_not_probe_varz_once_the_token_is_known_bad(monkeypatch):
    """/varz's 401 would be an echo of the failure above it, and would add a
    second, wrong diagnosis ('not-admin') to a report about a bad token."""
    c = FakeClient(version={"version": "2.25.0"}, routes={"user": AuthError("Unauthorized")})
    rep = _doctor(monkeypatch, c)
    assert rep["problems"] == ["bad-token"]
    assert [x["check"] for x in rep["checks"]] == ["reachable", "token"]
    assert ("GET", f"{WEB}/varz") not in c.calls


def test_doctor_degrades_on_varz(monkeypatch):
    """Not an admin: everything above still answered, so this is degraded, not
    failed — and `status` must not tempt a caller into treating it as broken."""
    c = FakeClient(
        version={"version": "2.25.0"},
        routes={"user": {**ME, "admin": False}, f"{WEB}/varz": AuthError("Unauthorized")},
    )
    rep = _doctor(monkeypatch, c)
    assert rep["status"] == "degraded"
    assert rep["problems"] == ["not-admin"]
    assert _diag(rep, "token")["ok"] is True


def test_doctor_survives_a_404_from_varz_and_still_reports_everything(monkeypatch):
    """THE regression test. `probe_varz` caught AuthError and ApiError but not
    NotFoundError — and those two are siblings, so a server that does not serve
    /varz (it is on the WEB root; a proxy forwarding only /api hides it) made the
    404 escape every handler and blow up the whole doctor.

    The bar is not "varz degrades". It is that the reachable and token answers —
    the ones the operator actually came for — still arrive.
    """
    c = FakeClient(
        version={"version": "2.25.0"},
        routes={"user": ME},  # no /varz route -> FakeClient 404s it, like the real thing
    )
    rep = _doctor(monkeypatch, c)

    assert [x["check"] for x in rep["checks"]] == ["reachable", "token", "capacity"]
    assert _diag(rep, "reachable")["ok"] is True
    assert _diag(rep, "token")["ok"] is True, "the answer we came for survives the bonus failing"
    assert _diag(rep, "token")["login"] == "droneadmin"
    assert _diag(rep, "capacity")["ok"] is False
    assert rep["status"] == "degraded", "not `failed` — nothing the caller depends on is broken"
    assert rep["problems"] == [], "a 404 on a bonus probe is not a diagnosis"
    assert rep["credential"] and rep["notes"], "a complete report, not a stub"


def test_doctor_never_raises_for_any_probe_failure(monkeypatch):
    """The promise, asserted as a property rather than case by case: returning the
    report IS doctor's job, so there is no error for which raising is correct."""
    for boom in (
        NotFoundError("404"),
        ValidationError("bad"),
        NotImplementedOnServer("nope"),
        ApiError("kaboom", status=503),
        RuntimeError("something nobody predicted"),
    ):
        c = FakeClient(version={"version": "2.25.0"}, routes={"user": ME, f"{WEB}/varz": boom})
        rep = _doctor(monkeypatch, c)
        assert rep["status"] in ("ok", "degraded", "failed"), boom
        assert _diag(rep, "token")["ok"] is True, f"{boom!r} cost us the token answer"


def test_doctor_reports_the_unconfigured_case_instead_of_failing(monkeypatch):
    ctx, obj = mk(FakeClient())

    def boom(_obj):
        raise ConfigError("no profile 'default' configured.")

    monkeypatch.setattr(S, "_open_client", boom)
    S.doctor(ctx)
    rep = obj.emitter.emitted
    assert rep["status"] == "failed"
    assert rep["problems"] == ["not-configured"]
    assert rep["notes"], "even unconfigured, the capability notes are useful"


def test_doctor_notes_the_scm_probe_it_deliberately_did_not_run(monkeypatch):
    """Read-only is a promise: the only SCM probe is a POST (a sync). Say that the
    check was skipped and how the failure looks, rather than skipping silently."""
    c = FakeClient(version={"version": "2.25.0"}, routes={"user": ME, f"{WEB}/varz": VARZ})
    rep = _doctor(monkeypatch, c)
    joined = " ".join(rep["notes"])
    assert "/api/version" in joined and "/api/nodes" in joined
    assert "500" in joined and "Unauthorized" in joined
    assert not any(m == "POST" for m, _ in c.calls), "doctor must never write"


# ---- server version ---------------------------------------------------

def test_server_version_emits_the_web_root_answer(monkeypatch):
    c = FakeClient(version={"source": "src", "version": "2.25.0", "commit": "deadbeef"})
    ctx, obj = mk(c)
    monkeypatch.setattr(S, "_open_client", lambda o: (c, "tok"))
    S.version(ctx)
    assert obj.emitter.emitted["version"] == "2.25.0"
    assert ("GET", "/version") in c.calls, "/api/version is a 404 and must never be called"


def test_server_version_on_a_non_drone_host_says_so(monkeypatch):
    c = FakeClient(version="<html>nginx</html>")
    ctx, _ = mk(c)
    monkeypatch.setattr(S, "_open_client", lambda o: (c, "tok"))
    with pytest.raises(ApiError) as exc:
        S.version(ctx)
    assert "doctor" in str(exc.value)


# ---- server queue -----------------------------------------------------

STAGE = {"id": 7, "repo_id": 1, "build_id": 4, "number": 1, "name": "default",
         "status": "pending", "os": "linux", "arch": "amd64", "created": 1752700000}


def test_queue_lists_stages():
    ctx, obj = mk(queue=[STAGE])
    S.queue(ctx)
    assert obj.emitter.emitted == [STAGE]
    assert "build_id" in obj.emitter.columns, "stages, not builds — build_id is the join"


def test_empty_queue_is_an_empty_list_not_none():
    """`GET /api/queue` can answer 204/None; an agent must not have to guess."""
    ctx, obj = mk(queue=None)
    S.queue(ctx)
    assert obj.emitter.emitted == []


def test_queue_403_says_admin():
    ctx, _ = mk(queue=AuthError("Unauthorized"))
    with pytest.raises(AuthError) as exc:
        S.queue(ctx)
    assert "admin" in str(exc.value).lower()
    assert "Unauthorized" != str(exc.value)


def test_admin_scope_does_not_bury_the_scm_message():
    """If the SCM link is what broke, "you need admin" would be a lie that sends
    the operator to fix the wrong thing."""
    original = AuthError("the server rejected its own SCM credentials (HTTP 500 'Unauthorized').")
    ctx, _ = mk(queue=original)
    with pytest.raises(AuthError) as exc:
        S.queue(ctx)
    assert exc.value is original


# ---- user ls / info ---------------------------------------------------

USERS = [
    {"id": 1, "login": "droneadmin", "admin": True, "machine": False, "active": True, "email": "a@b.c"},
    {"id": 2, "login": "ci-bot", "admin": False, "machine": True, "active": True, "avatar": "http://x/a.png"},
    {"id": 3, "login": "blocked-bob", "admin": False, "machine": False, "active": False},
]


def test_user_ls_shapes_every_row():
    ctx, obj = mk(users=USERS)
    U.ls(ctx, admin=False, machine=False, inactive=False)
    rows = obj.emitter.emitted
    assert [r["login"] for r in rows] == ["droneadmin", "ci-bot", "blocked-bob"]
    assert rows[2]["active"] is False, "missing must not read as present"
    assert rows[1]["email"] is None, "absent email is null, never ''"


def test_user_ls_filters_are_client_side():
    """The handler ignores every query param — there is no server-side filter to
    delegate to, so these must work here or not at all."""
    ctx, obj = mk(users=USERS)
    U.ls(ctx, admin=True, machine=False, inactive=False)
    assert [r["login"] for r in obj.emitter.emitted] == ["droneadmin"]

    ctx, obj = mk(users=USERS)
    U.ls(ctx, admin=False, machine=True, inactive=False)
    assert [r["login"] for r in obj.emitter.emitted] == ["ci-bot"]

    ctx, obj = mk(users=USERS)
    U.ls(ctx, admin=False, machine=False, inactive=True)
    assert [r["login"] for r in obj.emitter.emitted] == ["blocked-bob"]


def test_user_ls_403_says_admin_not_forbidden():
    ctx, _ = mk(users=AuthError("Unauthorized"))
    with pytest.raises(AuthError) as exc:
        U.ls(ctx, admin=False, machine=False, inactive=False)
    assert "SYSTEM admin" in str(exc.value)


def test_user_info_coalesces_the_avatar_field():
    """The server emits `avatar`; the docs and drone-go say `avatar_url`."""
    ctx, obj = mk(**{"users/ci-bot": {"login": "ci-bot", "avatar_url": "http://x/a.png"}})
    U.info(ctx, login="ci-bot")
    assert obj.emitter.emitted["avatar"] == "http://x/a.png"


def test_user_info_unknown_login_explains_itself():
    ctx, _ = mk()  # every path 404s
    with pytest.raises(NotFoundError) as exc:
        U.info(ctx, login="ghost")
    assert "user ls" in str(exc.value)


# ---- user add ---------------------------------------------------------

def test_machine_add_surfaces_the_token_loudly():
    """The one and only time this value is ever visible. The hash is json:"-" —
    no later call, admin or not, can read it back."""
    created = {"id": 9, "login": "ci-bot", "machine": True, "admin": False, "active": True,
               "token": "s3cr3ttoken32charsxxxxxxxxxxxxxx"}
    ctx, obj = mk(users=created)
    U.add(ctx, login="ci-bot", machine=True, admin=False)
    out = obj.emitter.emitted
    assert out["token"] == "s3cr3ttoken32charsxxxxxxxxxxxxxx"
    assert "NEVER" in out["note"] and "SAVE THIS TOKEN NOW" in out["note"]


def test_machine_add_sends_the_verified_body():
    c = FakeClient(routes={"users": {"login": "ci-bot", "machine": True, "token": "t"}})
    ctx, _ = mk(c)
    U.add(ctx, login="ci-bot", machine=True, admin=True)
    assert c.body == {"login": "ci-bot", "machine": True, "admin": True}


def test_human_add_mints_no_token_and_says_why():
    ctx, obj = mk(users={"id": 10, "login": "alice", "machine": False, "active": True})
    U.add(ctx, login="alice", machine=False, admin=False)
    out = obj.emitter.emitted
    assert "token" not in out, "a human account has no token to show — an empty one would read as a value"
    assert "--machine" in out["note"]


def test_machine_add_without_a_token_in_the_response_does_not_pretend():
    """Silence here is unrecoverable, so it must not look like success."""
    ctx, obj = mk(users={"id": 9, "login": "ci-bot", "machine": True})
    U.add(ctx, login="ci-bot", machine=True, admin=False)
    out = obj.emitter.emitted
    assert out["token"] is None
    assert "recreate" in out["note"]


def test_user_add_403_says_admin():
    ctx, _ = mk(users=AuthError("Unauthorized"))
    with pytest.raises(AuthError) as exc:
        U.add(ctx, login="x", machine=True, admin=False)
    assert "admin" in str(exc.value).lower()


# ---- user rm ----------------------------------------------------------

def test_rm_with_yes_deletes():
    c = FakeClient(routes={"users/ci-bot": None})
    ctx, obj = mk(c)
    U.rm(ctx, login="ci-bot", yes=True)
    assert ("DELETE", "users/ci-bot") in c.calls
    assert obj.emitter.emitted == {"status": "deleted", "login": "ci-bot"}


def test_rm_confirms_before_deleting(monkeypatch):
    """No undo, and the blast radius reaches their repos — never silent."""
    asked: list[str] = []

    def fake_confirm(text, abort=False):
        asked.append(text)
        raise typer.Abort()

    monkeypatch.setattr(U.typer, "confirm", fake_confirm)
    c = FakeClient(routes={"users/ci-bot": None})
    ctx, _ = mk(c)
    with pytest.raises(typer.Abort):
        U.rm(ctx, login="ci-bot", yes=False)
    assert "transferred" in asked[0]
    assert c.calls == [], "aborting must not have deleted anything"


def test_rm_unknown_login_is_a_clean_not_found():
    ctx, _ = mk()
    with pytest.raises(NotFoundError) as exc:
        U.rm(ctx, login="ghost", yes=True)
    assert "ghost" in str(exc.value)


# ---- user update: the tri-state ---------------------------------------
#
# Everything below defends one property: a flag the caller did not pass must not
# appear in the PATCH body. Drone's handler applies exactly the keys it receives,
# so an over-eager body is not cosmetic — it is a privilege change nobody asked
# for, delivered by a command that reported success.

ALICE = {"id": 4, "login": "alice", "admin": False, "machine": False, "active": True}


def _update_client(**overrides):
    """A server that echoes the patch back, i.e. one that actually applied it."""
    stored = {**ALICE, **overrides}

    class Echo(FakeClient):
        def patch(self, path, **kw):
            self.body = kw.get("json")
            self.calls.append(("PATCH", path))
            return {**stored, **(kw.get("json") or {})}

    return Echo(routes={"user": ME})


def test_update_omits_every_flag_the_caller_did_not_pass():
    """THE test in this section. `--admin` alone must not also ship
    active/machine — a body mentioning them would overwrite whatever alice has."""
    c = _update_client()
    ctx, _ = mk(c)
    U.update(ctx, login="alice", admin=True, active=None, machine=None, yes=False)
    assert c.body == {"admin": True}, "unset flags must be ABSENT, not false"


def test_update_sends_false_when_false_is_explicit():
    """The other half of tri-state: --no-machine must really send false, or the
    flag would be unimplementable."""
    c = _update_client(machine=True)
    ctx, _ = mk(c)
    U.update(ctx, login="ci-bot", admin=None, active=None, machine=False, yes=False)
    assert c.body == {"machine": False}


def test_update_sends_all_three_when_all_three_are_given():
    c = _update_client()
    ctx, _ = mk(c)
    U.update(ctx, login="alice", admin=False, active=True, machine=True, yes=True)
    assert c.body == {"admin": False, "active": True, "machine": True}


def test_update_with_no_flags_refuses_instead_of_sending_an_empty_patch():
    """An empty PATCH is a 200 that changed nothing — success-shaped silence."""
    c = _update_client()
    ctx, _ = mk(c)
    with pytest.raises(ValidationError) as exc:
        U.update(ctx, login="alice", admin=None, active=None, machine=None, yes=False)
    assert "--admin" in str(exc.value)
    assert c.calls == [], "and it must not have touched the server to find out"


def test_update_patches_by_login_and_shapes_the_answer():
    c = _update_client()
    ctx, obj = mk(c)
    U.update(ctx, login="alice", admin=True, active=None, machine=None, yes=False)
    assert ("PATCH", "users/alice") in c.calls
    out = obj.emitter.emitted
    assert out["login"] == "alice" and out["admin"] is True
    assert out["updated"] == ["admin"]


def test_update_says_privilege_change_out_loud():
    c = _update_client()
    ctx, obj = mk(c)
    U.update(ctx, login="alice", admin=True, active=None, machine=None, yes=False)
    note = obj.emitter.emitted["note"]
    assert "PRIVILEGE CHANGE" in note
    assert "every repo" in note, "say what admin actually buys, not just that it changed"


def test_update_note_is_absent_when_no_privilege_moved():
    c = _update_client()
    ctx, obj = mk(c)
    U.update(ctx, login="alice", admin=None, active=None, machine=True, yes=False)
    assert "note" not in obj.emitter.emitted


def test_update_reports_a_field_the_server_silently_dropped():
    """The `repo update` failure mode, one route over: 200 with the OLD value.
    Echoing what we sent would invent an outcome the server never agreed to."""
    c = FakeClient(routes={"user": ME, "users/alice": {**ALICE, "admin": False}})
    ctx, obj = mk(c)
    U.update(ctx, login="alice", admin=True, active=None, machine=None, yes=False)
    out = obj.emitter.emitted
    assert out["admin"] is False, "report the server's answer, not our request"
    assert "did not apply admin" in out["warning"]


def test_update_403_says_admin_not_forbidden():
    c = FakeClient(routes={"user": ME, "users/alice": AuthError("Unauthorized")})
    ctx, _ = mk(c)
    with pytest.raises(AuthError) as exc:
        U.update(ctx, login="alice", admin=True, active=None, machine=None, yes=False)
    assert "SYSTEM admin" in str(exc.value)
    assert "Unauthorized" != str(exc.value)


def test_update_unknown_login_explains_itself():
    c = FakeClient(routes={"user": ME})  # users/ghost 404s
    ctx, _ = mk(c)
    with pytest.raises(NotFoundError) as exc:
        U.update(ctx, login="ghost", admin=True, active=None, machine=None, yes=False)
    assert "user ls" in str(exc.value)


# ---- user update: the self-lockout guard ------------------------------


@pytest.fixture
def refuse_confirm(monkeypatch):
    """Every prompt aborts, so any test that reaches one fails loudly."""
    asked: list[str] = []

    def fake_confirm(text, abort=False):
        asked.append(text)
        raise typer.Abort()

    monkeypatch.setattr(U.typer, "confirm", fake_confirm)
    return asked


def test_demoting_yourself_confirms_first(refuse_confirm):
    """You cannot undo this: `user update` is admin-only, so revoking your own
    admin bit revokes your access to the route that would give it back."""
    c = _update_client()
    ctx, _ = mk(c)
    with pytest.raises(typer.Abort):
        U.update(ctx, login="droneadmin", admin=False, active=None, machine=None, yes=False)
    assert "cannot undo this yourself" in refuse_confirm[0]
    assert ("PATCH", "users/droneadmin") not in c.calls, "aborting must not have patched"


def test_blocking_yourself_confirms_too(refuse_confirm):
    """--no-active is the worse of the two: the token dies everywhere, not just
    on the admin routes."""
    c = _update_client()
    ctx, _ = mk(c)
    with pytest.raises(typer.Abort):
        U.update(ctx, login="droneadmin", admin=None, active=False, machine=None, yes=False)
    assert "--no-active" in refuse_confirm[0]


def test_the_guard_is_case_insensitive(refuse_confirm):
    """Logins are case-sensitive to Drone, so this over-triggers on purpose: a
    spurious prompt costs a keystroke, a missed one costs an account."""
    c = _update_client()
    ctx, _ = mk(c)
    with pytest.raises(typer.Abort):
        U.update(ctx, login="DroneAdmin", admin=False, active=None, machine=None, yes=False)
    assert refuse_confirm


def test_yes_skips_the_guard_for_the_caller_who_means_it():
    """Handing over admin is legitimate. Refusing outright would be this CLI
    deciding, wrongly, that it knows there are no other admins."""
    c = _update_client(admin=True)
    ctx, obj = mk(c)
    U.update(ctx, login="droneadmin", admin=False, active=None, machine=None, yes=True)
    assert c.body == {"admin": False}
    assert obj.emitter.emitted["admin"] is False


def test_demoting_someone_else_never_prompts(refuse_confirm):
    c = _update_client()
    ctx, _ = mk(c)
    U.update(ctx, login="alice", admin=False, active=None, machine=None, yes=False)
    assert refuse_confirm == [], "only your OWN lockout is a surprise"
    assert c.body == {"admin": False}


def test_promoting_yourself_never_prompts(refuse_confirm):
    """The guard is about losing access, not about touching your own row."""
    c = _update_client()
    ctx, _ = mk(c)
    U.update(ctx, login="droneadmin", admin=True, active=None, machine=None, yes=False)
    assert refuse_confirm == []


def test_a_broken_whoami_probe_does_not_block_the_update(refuse_confirm):
    """The probe is a courtesy. If /api/user is unhappy, an admin's legitimate
    PATCH must still go through — a guard that fails closed on an unrelated
    endpoint is a worse bug than the one it guards."""
    c = FakeClient(routes={"user": ApiError("cannot reach the server"),
                           "users/alice": {**ALICE, "admin": False}})
    ctx, _ = mk(c)
    U.update(ctx, login="alice", admin=False, active=None, machine=None, yes=False)
    assert ("PATCH", "users/alice") in c.calls
    assert refuse_confirm == []


def test_the_guard_costs_nothing_when_no_flag_can_lock_you_out():
    """No probe at all for a body that only grants: one fewer request, and the
    guard cannot misfire on a command that takes nothing away."""
    c = _update_client()
    ctx, _ = mk(c)
    U.update(ctx, login="droneadmin", admin=None, active=True, machine=None, yes=False)
    assert ("GET", "user") not in c.calls


# ---- user feed --------------------------------------------------------
#
# The endpoint is `/api/user/builds` and it returns REPOS. Verified live. Every
# test here exists because that name will mislead the next person to read it.

FEED = [
    {
        "id": 1, "namespace": "acme", "name": "api", "slug": "acme/api",
        "branch": "main", "active": True,
        "build": {"number": 42, "status": "failure", "event": "push", "target": "release/2.0",
                  "after": "42f7a46e"},
    },
    {
        "id": 2, "namespace": "acme", "name": "web", "slug": "acme/web",
        "branch": "main", "active": True,
        "build": {"number": 7, "status": "success", "event": "cron", "target": "main"},
    },
]


def test_feed_hoists_the_latest_build_of_every_repo():
    ctx, obj = mk(**{"user/builds": FEED})
    U.feed(ctx)
    rows = obj.emitter.emitted
    assert [r["slug"] for r in rows] == ["acme/api", "acme/web"]
    assert rows[0]["last_build_status"] == "failure"
    assert rows[0]["last_build_number"] == 42
    assert rows[0]["last_build_event"] == "push"


def test_feed_reads_the_build_branch_from_target_not_branch():
    """A build's branch is `target`. `build.branch` does not exist, and reaching
    for it would hand back None for every row."""
    ctx, obj = mk(**{"user/builds": FEED})
    U.feed(ctx)
    assert obj.emitter.emitted[0]["last_build_branch"] == "release/2.0"


def test_feed_never_clobbers_the_repos_own_branch():
    """`branch` on a repo is its DEFAULT branch; hoisting the build's branch onto
    that key would overwrite a real field with a different meaning — and the two
    differ exactly when the feed is worth reading."""
    ctx, obj = mk(**{"user/builds": FEED})
    U.feed(ctx)
    row = obj.emitter.emitted[0]
    assert row["branch"] == "main", "the repo's default branch survives"
    assert row["last_build_branch"] == "release/2.0"


def test_feed_keeps_the_nested_build_object():
    """Hoist, don't allowlist: the full build stays for anyone projecting it."""
    ctx, obj = mk(**{"user/builds": FEED})
    U.feed(ctx)
    assert obj.emitter.emitted[0]["build"]["after"] == "42f7a46e"


def test_feed_columns_are_the_fleet_health_table():
    ctx, obj = mk(**{"user/builds": FEED})
    U.feed(ctx)
    assert obj.emitter.columns == [
        "slug", "last_build_status", "last_build_number", "last_build_event", "last_build_branch",
    ]


def test_feed_field_names_match_repo_ls_latest():
    """Both surfaces answer "what is the fleet doing" from the same shape. An
    agent that learned `last_build_status` from one must not relearn it here."""
    from dronecli.commands import repo as R

    theirs = R._decorate({"slug": "acme/api", "build": {"status": "failure", "number": 42}}, False)
    ours = U._feed_row(FEED[0])
    assert theirs["last_build_status"] == ours["last_build_status"]
    assert theirs["last_build_number"] == ours["last_build_number"]


def test_feed_repo_with_no_build_is_a_row_of_nulls_not_a_crash():
    """`build` is absent or null (never {}) for a repo that never ran — naive
    hoisting NPEs, and dropping the row hides "enabled but never built", which is
    a genuine fleet-health answer."""
    ctx, obj = mk(**{"user/builds": [{"slug": "acme/idle", "branch": "main"},
                                     {"slug": "acme/null", "build": None}]})
    U.feed(ctx)
    rows = obj.emitter.emitted
    assert len(rows) == 2
    assert rows[0]["last_build_status"] is None and rows[0]["last_build_number"] is None
    assert rows[1]["last_build_branch"] is None


def test_feed_derives_a_missing_slug_from_namespace_and_name():
    ctx, obj = mk(**{"user/builds": [{"namespace": "acme", "name": "api", "build": {"number": 1}}]})
    U.feed(ctx)
    assert obj.emitter.emitted[0]["slug"] == "acme/api"


def test_empty_feed_is_an_empty_list_not_none():
    ctx, obj = mk(**{"user/builds": None})
    U.feed(ctx)
    assert obj.emitter.emitted == []


def test_feed_is_not_admin_gated_and_must_not_claim_otherwise():
    """It reads /api/user (singular) — the ONE route in this module that anyone
    can call. Blaming a failure on the admin bit would send the caller to fix a
    permission that was never involved."""
    ctx, _ = mk(**{"user/builds": AuthError("Unauthorized")})
    with pytest.raises(AuthError) as exc:
        U.feed(ctx)
    assert "admin" not in str(exc.value).lower()


def test_feed_asks_the_singular_endpoint_once_and_does_not_page():
    """The handler ignores page/per_page and returns the whole array, so paging
    it would refetch page 1 forever."""
    c = FakeClient(routes={"user/builds": FEED})
    ctx, _ = mk(c)
    U.feed(ctx)
    assert c.calls == [("GET", "user/builds")]
