"""`drone-cli server` — is this server healthy, and which token is speaking?

`doctor` is why this module exists. Drone collapses six unrelated failures into
one indistinguishable HTTP 401 "Unauthorized" (or, worse, a 500 that says
"Unauthorized" and means something else entirely). Each one has a *different
fix*, and telling them apart today requires knowing that `/api/version` is a 404,
that `/healthz` lies, and that `GET /api/user` is the only honest token probe.
This chains the probes once and names the failure.

**Every probe below must return, never raise.** `doctor` returning the report IS
its job — an escaping exception leaves the operator with a traceback and no
diagnosis, i.e. exactly where they were before they ran it. Two things make that
easy to get wrong, so both are handled belt-and-braces here:

* In :mod:`agentcli.errors`, ``NotFoundError`` and ``ApiError`` are **siblings**
  (both subclass ``OpError`` directly) — ``except ApiError`` does NOT catch a 404.
  Every probe therefore ends in an ``except OpError`` floor rather than trusting
  that the named branches are exhaustive.
* :func:`_run_probe` catches whatever still escapes, so a probe added later
  cannot break the promise by forgetting.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import typer
from agentcli.errors import ApiError, AuthError, ConfigError, NotFoundError, OpError

from ..client import Client
from ..spec import SPEC, credentials
from ._shared import ctx_obj

app = typer.Typer(no_args_is_help=True)

_QUEUE_COLUMNS = ["id", "build_id", "number", "name", "status", "os", "arch", "created"]

# client.py maps Drone's 500-with-body-{"message":"Unauthorized"} — the SCM link
# being dead, NOT the caller's token — onto AuthError with this phrase in it.
# That mapping is client.py's to own; this is the seam where doctor reads it back
# so it can name the diagnosis instead of re-deriving it from a status code it no
# longer has.
_SCM_500_MARKER = "SCM credentials"

# Facts an agent would otherwise learn by probing (and would probe wrong: three of
# these four are 404s that look like "you got the path wrong"). Reported, never
# probed — doctor must stay read-only and side-effect free, so it is safe to run
# against production when everything is already on fire.
_CAPABILITY_NOTES = [
    "GET /api/version does NOT exist (404). The version endpoint is GET /version on the WEB root, unauthenticated.",
    "GET /api/nodes does NOT exist (404). There is no node/agent listing in this server; `server queue` is the closest thing.",
    "POST .../lint and POST .../verify do NOT exist (404) in Drone 2. POST .../sign and POST .../encrypt do.",
    "GET /healthz answers 200 early — it proves a process is listening, not that the DB migrated or the bootstrap ran. GET /api/user is the real readiness probe.",
    "GET /metrics lives on the web root (not /api/metrics) and 401s unless DRONE_PROMETHEUS_ANONYMOUS_ACCESS=true.",
]


def is_scm_link_error(exc: Exception) -> bool:
    """Is this the 500-'Unauthorized' SCM failure rather than a real 401/403?"""
    return _SCM_500_MARKER in str(exc)


@contextmanager
def admin_scope(what: str) -> Iterator[None]:
    """Turn the 403 behind AuthorizeAdmin into a sentence that names the cause.

    Lives here rather than in `_shared.py` because it encodes *this* module's
    knowledge — that Drone's admin 403 and its SCM-500 arrive as the same
    exception type and must not be given the same message. `user.py` is its only
    other caller, and it is the other half of the admin surface.
    """
    try:
        yield
    except AuthError as exc:
        if is_scm_link_error(exc):
            raise  # already the best message in the codebase; do not bury it
        raise AuthError(
            f"{what} requires a Drone SYSTEM admin (user.admin = true) — that is a different "
            f"thing from repo admin, and it bypasses repo permissions entirely. Check which "
            f"account you are: `drone-cli auth whoami`. Diagnose: `drone-cli server doctor`.",
            detail=getattr(exc, "detail", None),
        ) from exc


def _open_client(obj) -> tuple[Client, str | None]:
    """A client that works with NO token.

    `AppContext.client()` refuses to build without one — correct for every other
    command, fatal here: "there is no token" and "the token is wrong" are two of
    the states doctor exists to tell apart, and `/version` is unauthenticated
    precisely so it can answer in both.
    """
    prof = obj.config.resolve()
    token = credentials.get_token(obj.config.active_profile_name())
    return Client(prof.base_url, token or "", verify_ssl=prof.verify_ssl), token


# ---------------------------------------------------------------------------
# probes — pure over a client, so each failure maps to exactly one diagnosis
# ---------------------------------------------------------------------------


def _run_probe(check: str, fn) -> dict:
    """Run a probe and turn ANYTHING that escapes into a reported failure.

    The probes each name what they can, and end in an `except OpError` floor. This
    is the floor under *that*: doctor's whole contract is that it hands back a
    report, and the cost of a probe leaking is not a bad row — it is the operator
    getting a traceback instead of the diagnosis for a server that is already on
    fire. A bare `except Exception` is the right blast radius here precisely
    because doctor promises a report unconditionally; the report says which probe
    broke, so the gap is visible rather than swallowed.
    """
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — deliberate; see the docstring
        return {
            "check": check, "ok": False, "diagnosis": "probe-failed",
            "message": (
                f"the {check} probe itself raised {type(exc).__name__}: {exc}. That is a gap in "
                f"this CLI, not proof of anything about the server — the other checks still hold."
            ),
        }


def probe_version(client) -> dict:
    """Probe 1: is anything there, and is it Drone?"""
    try:
        res = client.version()
    except NotFoundError as exc:
        return {
            "check": "reachable", "ok": False, "diagnosis": "not-a-drone-server",
            "message": (
                f"{client.web_root} answered, but GET /version is a 404 — every Drone serves it "
                f"unauthenticated. This is some other service, or a proxy that is not routing to "
                f"Drone. Server said: {exc}"
            ),
        }
    except AuthError:
        return {
            "check": "reachable", "ok": False, "diagnosis": "not-a-drone-server",
            "message": (
                f"{client.web_root}/version demanded authentication. Drone never does — something "
                f"in front of it (a proxy, an SSO gateway) is intercepting the request."
            ),
        }
    except ApiError as exc:
        return {
            "check": "reachable", "ok": False, "diagnosis": "unreachable",
            "message": f"cannot reach {client.web_root}: {exc}. Check the URL, DNS, TLS and that the server is up.",
        }
    except OpError as exc:
        # The floor: a 400 or a 501 from /version reaches here, since neither is
        # an ApiError (they are siblings). Something answered and it did not
        # answer the way any Drone does — that is a diagnosis, not a crash.
        return {
            "check": "reachable", "ok": False, "diagnosis": "not-a-drone-server",
            "message": (
                f"{client.web_root}/version answered, but not the way any Drone does "
                f"({type(exc).__name__}: {exc}). Every Drone serves it unauthenticated with a "
                f"JSON body."
            ),
        }

    if not isinstance(res, dict):
        # A wrong path (or a non-Drone host) hands back plain text, which
        # client.py passes through rather than exploding in json.loads.
        return {
            "check": "reachable", "ok": False, "diagnosis": "not-a-drone-server",
            "message": (
                f"{client.web_root}/version returned text, not JSON: {str(res).strip()[:120]!r}. "
                f"Whatever is at that URL, it is not a Drone server."
            ),
        }
    out = {
        "check": "reachable", "ok": True, "diagnosis": None,
        "message": f"{client.web_root} is a Drone server (version {res.get('version') or 'unreported'}).",
        "version": res.get("version"),
        "source": res.get("source"),
        "commit": res.get("commit"),
    }
    if not res.get("version"):
        # Every field on Drone's version payload is omitempty, so an empty object
        # is legal-ish -- but it is also what a stub/mock returns.
        out["warning"] = "the server answered /version without a version field. Assuming Drone, but verify."
    return out


def probe_token(client, has_token: bool) -> dict:
    """Probe 2: `GET /api/user` — the ONLY valid token probe.

    Not a matter of taste: `acl.AuthorizeUser` guards `/repos` only when
    DRONE_SERVER_PRIVATE_MODE is on, so on a default server a *public* repo reads
    200 with no token at all. Probing with `repo info` would call a broken login
    healthy.
    """
    if not has_token:
        return {
            "check": "token", "ok": False, "diagnosis": "no-token",
            "message": (
                "no token found in any credential backend. Run `drone-cli auth login`, or export "
                "DRONE_TOKEN. Read-only commands against public repos may still work without one, "
                "which is exactly how this goes unnoticed."
            ),
        }
    try:
        me = client.get("user")
    except NotFoundError as exc:
        # /api/user is a 404: not "you are logged out" — there is no Drone here,
        # or a proxy is not routing /api to it. Blaming the token would send the
        # caller to rotate a credential that was never the problem.
        return {
            "check": "token", "ok": False, "diagnosis": "not-a-drone-server",
            "message": (
                f"GET /api/user is a 404 on {client.web_root} — every Drone serves it. Something "
                f"else is answering at this URL, or a proxy is not routing /api through. "
                f"Server said: {exc}"
            ),
        }
    except AuthError as exc:
        if is_scm_link_error(exc):
            return {
                "check": "token", "ok": False, "diagnosis": "scm-link-broken",
                "message": str(exc), "detail": getattr(exc, "detail", None),
            }
        return {
            "check": "token", "ok": False, "diagnosis": "bad-token",
            "message": (
                f"the token was rejected by {client.web_root} (server said: {exc}). Drone tokens do "
                f"not expire and there is exactly one per user — so this is a wrong or rotated "
                f"token, not a stale session. Get the current one at {client.web_root}/account."
            ),
        }
    except ApiError as exc:
        return {
            "check": "token", "ok": False, "diagnosis": "unreachable",
            "message": f"GET /api/user failed: {exc}",
        }
    except OpError as exc:
        return {
            "check": "token", "ok": False, "diagnosis": "probe-failed",
            "message": (
                f"GET /api/user failed in a way this probe cannot classify "
                f"({type(exc).__name__}: {exc}). The token was neither accepted nor rejected, so "
                f"treat it as unverified rather than bad."
            ),
        }

    if not isinstance(me, dict) or not me.get("login"):
        return {
            "check": "token", "ok": False, "diagnosis": "not-a-drone-server",
            "message": f"GET /api/user returned something without a `login` field: {str(me)[:120]!r}.",
        }
    out = {
        "check": "token", "ok": True, "diagnosis": None,
        "message": f"authenticated as {me.get('login')}"
                   + (" (system admin)" if me.get("admin") else " (not an admin)")
                   + (" [machine account]" if me.get("machine") else ""),
        "login": me.get("login"),
        "admin": bool(me.get("admin")),
        "machine": bool(me.get("machine")),
        "active": bool(me.get("active")),
    }
    warns = []
    if not me.get("admin"):
        warns.append(
            "not a system admin: `user *`, `server queue` and several `repo update` fields will "
            "403 — and some PATCH fields are silently DROPPED (200, unchanged values) instead."
        )
    if me.get("active") is False:
        warns.append("this account is blocked (active=false). Most calls will fail.")
    if warns:
        out["warning"] = " ".join(warns)
    return out


def probe_varz(client) -> dict:
    """Probe 3: `/varz` — SCM rate budget and license seats.

    Degrades on purpose: it may be admin-gated, missing, or proxied away, and a
    non-admin running doctor must still get every other answer. A missing bonus is
    not a failed doctor — so EVERY exit from here is a returned dict.
    """
    try:
        res = client.get(f"{client.web_root}/varz")
    except NotFoundError:
        # Not every deployment serves /varz: it sits on the WEB root, so a proxy
        # that only forwards /api makes it a 404. This used to escape the whole
        # doctor (NotFoundError is not an ApiError), which turned the one bonus
        # probe into the thing that denied the operator every other answer.
        return {
            "check": "capacity", "ok": False, "diagnosis": None,
            "message": (
                "/varz is not served at this URL (404). It lives on the web root, so a proxy that "
                "only routes /api hides it. SCM rate budget and license seats unknown — not fatal, "
                "everything else above still holds."
            ),
        }
    except AuthError as exc:
        if is_scm_link_error(exc):
            return {
                "check": "capacity", "ok": False, "diagnosis": "scm-link-broken",
                "message": str(exc), "detail": getattr(exc, "detail", None),
            }
        return {
            "check": "capacity", "ok": False, "diagnosis": "not-admin",
            "message": "/varz is not readable with this token (it is admin-gated here). "
                       "SCM rate budget and license seats unknown — everything else above still holds.",
        }
    except ApiError as exc:
        return {
            "check": "capacity", "ok": False, "diagnosis": None,
            "message": f"/varz did not answer ({exc}). Not fatal: it is a bonus, not a dependency.",
        }
    except OpError as exc:
        return {
            "check": "capacity", "ok": False, "diagnosis": None,
            "message": (
                f"/varz did not answer ({type(exc).__name__}: {exc}). Not fatal: it is a bonus, "
                f"not a dependency."
            ),
        }

    if not isinstance(res, dict):
        return {
            "check": "capacity", "ok": False, "diagnosis": None,
            "message": "/varz returned a non-JSON body; skipping capacity checks.",
        }

    scm = res.get("scm") or {}
    rate = scm.get("rate") or {}
    lic = res.get("license") or {}
    out = {
        "check": "capacity", "ok": True, "diagnosis": None,
        "message": "read /varz.",
        "scm_url": scm.get("url"),
        "scm_rate_limit": rate.get("limit"),
        "scm_rate_remaining": rate.get("remaining"),
        "scm_rate_reset": rate.get("reset"),
        "license_kind": lic.get("kind"),
        "license_seats": lic.get("seats"),
        "license_seats_used": lic.get("seats_used"),
    }
    remaining = rate.get("remaining")
    limit = rate.get("limit")
    if isinstance(remaining, int) and isinstance(limit, int) and limit > 0:
        if remaining <= 0:
            out["ok"] = False
            out["diagnosis"] = "scm-quota-exhausted"
            out["message"] = (
                f"the server's SCM API budget is exhausted (0 of {limit} left, resets at "
                f"{rate.get('reset')}). Sync, enable and repair will fail until it resets — and "
                f"they fail as HTTP 500 'Unauthorized', which looks nothing like a rate limit."
            )
        elif remaining < max(1, limit // 10):
            out["warning"] = f"SCM API budget nearly gone: {remaining} of {limit} left."

    seats, used = lic.get("seats"), lic.get("seats_used")
    if isinstance(seats, int) and isinstance(used, int) and seats > 0 and used >= seats:
        out["warning"] = (
            f"license seats full ({used}/{seats}). `user add` returns HTTP 402 in this state."
        )
    return out


def _credential_report(obj) -> dict:
    """WHICH credential is actually speaking — the answer people lose hours to.

    Precedence is env > keyring > file (deliberate: it is what makes CI work), so
    an exported DRONE_TOKEN silently beats a keyring login. Worse, DRONE_* is the
    namespace the Drone runner injects into every build step, so a pipeline can
    authenticate as someone else entirely and never say so.
    """
    out = {
        "backend": credentials.backend_name(),
        "profile": obj.config.active_profile_name(),
        "tokenEnvVars": list(SPEC.token_env_names()),
    }
    hit = credentials._env_token_hit()
    if hit:
        out["note"] = (
            f"you are authenticating with ${hit[0]}, NOT your keyring — the environment always "
            f"wins. If you logged in with `drone-cli auth login` and are seeing the wrong user, "
            f"this is why. `unset {hit[0]}` to use the stored login."
        )
    return out


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


@app.command()
def version(ctx: typer.Context) -> None:
    """Show the Drone server's version — `GET /version` on the WEB root.

    NOT `/api/version`, which is a 404 and does not exist despite looking like it
    should. Unauthenticated, so this answers even when your token is wrong: it is
    the reachability/compat probe, not an auth check.
    """
    obj = ctx_obj(ctx)
    client, _ = _open_client(obj)
    res = client.version()
    if not isinstance(res, dict):
        raise ApiError(
            f"{client.web_root}/version did not return JSON — this may not be a Drone server. "
            f"Run `drone-cli server doctor` for a full diagnosis. Body: {str(res).strip()[:120]!r}"
        )
    obj.emitter.emit(
        {"server": client.web_root, "version": res.get("version"), "source": res.get("source"),
         "commit": res.get("commit")},
        columns=["server", "version", "source", "commit"],
    )


@app.command()
def doctor(ctx: typer.Context) -> None:
    """Diagnose this server, this token and this SCM link — and NAME the failure.

    Run this first when anything is confusing. Drone renders six unrelated
    problems as one "401 Unauthorized"; each of these has a different fix:

      unreachable          the URL/DNS/TLS is wrong, or the server is down
      not-a-drone-server   something answered, but it isn't Drone (proxy? wrong port?)
      no-token             nothing is authenticating you at all
      bad-token            your token is wrong (they never expire — so it's wrong, not stale)
      not-admin            your token is fine; the route is behind AuthorizeAdmin
      scm-link-broken      YOUR TOKEN IS FINE. The *server's* login to Gitea/GitHub is
                           dead, and Drone reports that as HTTP 500 "Unauthorized"
      scm-quota-exhausted  the SCM's API budget ran out; syncs fail (as a 500, naturally)
      probe-failed         the CHECK broke, not (necessarily) the server. Says nothing
                           about the thing it was probing — read its `message`.

    Also reports WHICH credential is in use: an exported $DRONE_TOKEN silently
    overrides a keyring login, and the Drone runner injects DRONE_* into every
    build step.

    Read-only and side-effect free (nothing is POSTed), and it never exits
    non-zero for a failed probe — a report IS the deliverable. Check `status` and
    `problems` in the JSON, not the exit code.
    """
    obj = ctx_obj(ctx)

    try:
        client, token = _open_client(obj)
    except ConfigError as exc:
        # Doctor is the command you run when things are broken; a broken config is
        # a finding to report, not a reason to refuse to report.
        obj.emitter.emit(
            {
                "status": "failed",
                "credential": {"backend": credentials.backend_name()},
                "checks": [{"check": "config", "ok": False, "diagnosis": "not-configured", "message": str(exc)}],
                "problems": ["not-configured"],
                "notes": _CAPABILITY_NOTES,
            }
        )
        return

    checks = [_run_probe("reachable", lambda: probe_version(client))]
    tok = _run_probe("token", lambda: probe_token(client, has_token=bool(token)))
    checks.append(tok)
    # Only ask about capacity once we know we are talking to a Drone that knows
    # us -- otherwise /varz's failure is just an echo of the failure above and
    # adds a second, misleading diagnosis to the report.
    if checks[0]["ok"] and tok["ok"]:
        checks.append(_run_probe("capacity", lambda: probe_varz(client)))

    problems = [c["diagnosis"] for c in checks if c.get("diagnosis")]
    warnings = [c["warning"] for c in checks if c.get("warning")]
    fatal = [c for c in checks if not c["ok"] and c["check"] in ("reachable", "token")]
    # A check that failed without earning a named diagnosis (an unserved /varz) is
    # still not "ok" -- saying `status: ok` over a row of `ok: false` would invite
    # a caller to trust a report we know is incomplete.
    degraded = bool(problems or warnings) or any(not c["ok"] for c in checks)

    report = {
        "status": "failed" if fatal else ("degraded" if degraded else "ok"),
        "server": client.web_root,
        "credential": _credential_report(obj),
        "checks": checks,
        "problems": problems,
        "warnings": warnings,
        "notes": _CAPABILITY_NOTES + [
            "the server's SCM link is NOT probed here: the only probe is POST /api/user/repos (a "
            "sync), and doctor stays read-only. If enable/sync/repair return HTTP 500 "
            "'Unauthorized', that is this failure — your token is fine, the server's is not."
        ],
    }
    obj.emitter.emit(report)


@app.command()
def queue(ctx: typer.Context) -> None:
    """Show work the server has not finished yet — `GET /api/queue`. ADMIN ONLY.

    SHAPE TRAP: these are **stages** (`[]core.Stage`), not builds. One build with
    three parallel stages appears three times; `build_id` is what ties them
    together, and `number` is the stage's 1-based ordinal within its build, not a
    build number.

    A row sitting at `pending` with no `machine` means nothing has claimed it —
    usually no runner is connected, or none matches its `os`/`arch`. There is no
    way to check runners directly: `GET /api/nodes` does not exist (404), despite
    the client libraries declaring it.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    with admin_scope("`server queue` (GET /api/queue)"):
        rows = client.get("queue")
    obj.emitter.emit(rows or [], columns=_QUEUE_COLUMNS)
