"""`drone-cli user` — the ADMIN user directory, plus your own build feed.

Note the one-character path difference Drone hangs an entire privilege boundary
off: `/api/user` (singular) is **you**, `/api/users` (plural) is **everyone** and
is behind `AuthorizeAdmin`. Everything in this group is the plural, admin-only
one — 403 without system admin — with exactly one exception, `user feed`, which
reads the singular path and works for anybody. For your own identity, use
`drone-cli auth whoami`.

"Admin" here means **system** admin (`user.admin = true`), which is orthogonal to
repo permissions and bypasses them entirely — being a repo admin buys you nothing
in this group.
"""

from __future__ import annotations

import typer
from agentcli.errors import NotFoundError, OpError, ValidationError

from ._shared import ctx_obj
from .server import admin_scope

app = typer.Typer(no_args_is_help=True)

_COLUMNS = ["id", "login", "email", "admin", "machine", "active", "last_login"]

#: `GET /api/user/builds` answers with REPO objects, so the feed is shaped like
#: `repo ls --latest` and deliberately uses that command's `last_build_*` names:
#: an agent that learned the field names from one must not have to relearn them
#: for the other. They are NOT called `event`/`branch` because a repo object
#: already HAS a `branch` (its default branch) — hoisting the build's branch onto
#: that key would overwrite a real field with a different meaning.
_FEED_COLUMNS = [
    "slug",
    "last_build_status",
    "last_build_number",
    "last_build_event",
    "last_build_branch",
]


def _shape(user: dict) -> dict:
    """One user, with the fields an agent branches on made explicitly boolean.

    The server never emits the token hash (`json:"-"`), so there is nothing to
    redact here — but do not add one: a user's token is unreadable by design,
    even for an admin (see `user add --machine`).
    """
    return {
        "id": user.get("id"),
        "login": user.get("login"),
        "email": user.get("email") or None,
        "admin": bool(user.get("admin")),
        "machine": bool(user.get("machine")),
        "active": bool(user.get("active")),
        "created": user.get("created"),
        "updated": user.get("updated"),
        "last_login": user.get("last_login"),
        # Drone emits `avatar`; the docs and drone-go say `avatar_url`. Coalesce
        # so callers never have to know which server they hit.
        "avatar": user.get("avatar") or user.get("avatar_url") or None,
        "syncing": bool(user.get("syncing")),
    }


def _tri_state_body(*, admin: bool | None, active: bool | None, machine: bool | None) -> dict:
    """The PATCH body — flags the caller never mentioned are OMITTED, not false.

    This is the whole reason `update`'s flags default to `None` rather than
    `False`. Drone's update handler decodes into pointers (`*bool`) and applies
    only the keys that are present, so an omitted key preserves the stored value
    while `false` overwrites it. Sending the unset flags as false would mean
    `user update alice --no-active` silently strips alice's admin bit too — a
    privilege change nobody asked for, in a body nobody read.
    """
    body: dict = {}
    if admin is not None:
        body["admin"] = admin
    if active is not None:
        body["active"] = active
    if machine is not None:
        body["machine"] = machine
    return body


def _my_login(client) -> str | None:
    """Who this token is, or None if we could not find out.

    `/api/user` is the singular path — it is not admin-gated, so this costs one
    cheap GET and works even while the caller is losing the admin bit. Failure
    returns None on purpose: this probe exists to raise a confirmation prompt,
    and letting a hiccup on an unrelated endpoint block an admin's legitimate
    PATCH would be a worse bug than the one it guards against.
    """
    try:
        me = client.get("user")
    except OpError:
        return None
    return (me or {}).get("login") if isinstance(me, dict) else None


def _guard_self_lockout(client, login: str, *, admin: bool | None, active: bool | None,
                        yes: bool) -> None:
    """Confirm before a caller revokes their OWN access.

    Two flags lock you out of the very route you would need to undo them, because
    `user update` is itself admin-only:

      --no-admin   you keep your login, but every command in this group starts
                   answering 403. Only another admin can give it back.
      --no-active  worse: the account is blocked outright, so the token stops
                   working everywhere, not just here.

    Confirm rather than refuse. Refusing outright would be wrong — a bootstrap
    admin dropping privileges after handing over is a real, correct thing to do,
    and it is not this CLI's business to decide there is another admin left.
    Doing it silently would be worse: the flag reads like any other toggle and
    gives no hint that it is one-way. So: prompt, name the consequence, and let
    `--yes` through for the caller who means it (and for automation).

    Login comparison is casefolded even though Drone treats logins as
    case-sensitive elsewhere. A false positive costs one prompt; a false negative
    costs an admin their account.
    """
    if yes or (admin is not False and active is not False):
        return
    me = _my_login(client)
    if not me or me.casefold() != (login or "").casefold():
        return

    what = "block your own account (--no-active)" if active is False else \
        "remove your own system-admin bit (--no-admin)"
    typer.confirm(
        f"You are {me}. This will {what}. `user update` is itself admin-only, so you "
        f"cannot undo this yourself — another admin would have to. Continue?",
        abort=True,
    )


@app.command("ls")
def ls(
    ctx: typer.Context,
    admin: bool = typer.Option(False, "--admin", help="Only system admins."),
    machine: bool = typer.Option(False, "--machine", help="Only machine (bot) accounts."),
    inactive: bool = typer.Option(False, "--inactive", help="Only blocked accounts (active=false)."),
) -> None:
    """List every Drone user. ADMIN ONLY (403 otherwise).

    The handler ignores every query parameter — no pagination, no filter, no sort,
    no search — so it returns one full array and the filters above are applied
    here, client-side. That is not a limitation to work around; it is the whole
    API.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    with admin_scope("`user ls` (GET /api/users)"):
        rows = client.get("users") or []

    out = [_shape(u) for u in rows]
    if admin:
        out = [u for u in out if u["admin"]]
    if machine:
        out = [u for u in out if u["machine"]]
    if inactive:
        out = [u for u in out if not u["active"]]
    obj.emitter.emit(out, columns=_COLUMNS)


@app.command("info")
def info(
    ctx: typer.Context,
    login: str = typer.Argument(..., help="The user's LOGIN (their SCM username), not a numeric id."),
) -> None:
    """Show one user. ADMIN ONLY (403 otherwise).

    Users are addressed by login everywhere in this API; the numeric `id` in the
    response is not usable as a path segment.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    with admin_scope(f"`user info {login}` (GET /api/users/{login})"):
        try:
            user = client.get(f"users/{login}")
        except NotFoundError as exc:
            raise NotFoundError(
                f"no Drone user '{login}'. Logins are SCM usernames and are case-sensitive here; "
                f"a user only exists in Drone once they have logged in (or been created with "
                f"`drone-cli user add`). List them: `drone-cli user ls`.",
                detail=getattr(exc, "detail", None),
            ) from exc
    obj.emitter.emit(_shape(user), columns=_COLUMNS)


@app.command("add")
def add(
    ctx: typer.Context,
    login: str = typer.Argument(..., help="The login to create. For a human, this MUST match their SCM username."),
    machine: bool = typer.Option(False, "--machine", help="Create a bot account and MINT ITS TOKEN (shown once, never again)."),
    admin: bool = typer.Option(False, "--admin", help="Grant system admin — bypasses every repo permission check."),
) -> None:
    """Create a user. ADMIN ONLY (403 otherwise).

    Two very different things behind one route:

      --machine   a bot account. Drone mints a token and RETURNS IT IN THIS ONE
                  RESPONSE. There is no second chance: the hash is `json:"-"`, so
                  no API call — not even as admin — can ever read it back. Lose it
                  and your only recovery is delete + recreate. Capture it:
                      drone-cli user add ci-bot --machine --fields token

      (human)     Drone calls the SCM to resolve the login, and may overwrite the
                  login/email you passed with what the SCM says. If the server's
                  SCM link is dead this fails as HTTP 500 "Unauthorized" — that is
                  the server's credentials, not yours (`drone-cli server doctor`).

    `active` cannot be set: the server hardcodes it true. HTTP 402 means the
    license seat limit is reached, not a bad request.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    body = {"login": login, "machine": machine, "admin": admin}
    with admin_scope("`user add` (POST /api/users)"):
        created = client.post("users", json=body)

    out = _shape(created)
    token = (created or {}).get("token")
    if machine:
        # Emitted at the TOP level and named loudly: this value exists for exactly
        # one HTTP response in the lifetime of the account.
        out["token"] = token
        out["note"] = (
            f"SAVE THIS TOKEN NOW — it is shown once and can NEVER be retrieved again, by anyone, "
            f"including admins. Use it as: DRONE_TOKEN=<token> drone-cli ... "
            f"If you lose it: drone-cli user rm {login} -y && drone-cli user add {login} --machine."
        ) if token else (
            "the server did not return a token for this machine account — unexpected. It cannot be "
            "read back later, so delete and recreate rather than assuming one exists."
        )
    else:
        out["note"] = (
            f"human account: no token is minted (only --machine mints one). {login} gets one by "
            f"logging in through the SCM. Fields may have been overwritten by the SCM's answer."
        )
    obj.emitter.emit(out, columns=_COLUMNS)


@app.command("update")
def update(
    ctx: typer.Context,
    login: str = typer.Argument(..., help="The user's LOGIN (their SCM username), not a numeric id."),
    admin: bool = typer.Option(
        None,
        "--admin/--no-admin",
        help="Grant/revoke SYSTEM admin. PRIVILEGE CHANGE: an admin bypasses every repo "
             "permission check on every repo. Unset = leave as-is.",
    ),
    active: bool = typer.Option(
        None,
        "--active/--no-active",
        help="Unblock/block the account. --no-active kills their token everywhere. Unset = leave as-is.",
    ),
    machine: bool = typer.Option(
        None,
        "--machine/--no-machine",
        help="Mark as a bot account. Does NOT mint a token — only `user add --machine` does. Unset = leave as-is.",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the self-lockout confirmation."),
) -> None:
    """Change a user's flags. ADMIN ONLY (403 otherwise).

        drone-cli user update alice --admin          # promote
        drone-cli user update ci-bot --no-active     # block a bot, keep its repos

    Every flag is TRI-STATE: pass `--admin` or `--no-admin` to set it, or leave it
    off entirely to leave it alone. An omitted flag is omitted from the PATCH
    body, never sent as false — this route patches only the keys it receives, so
    a body that mentions a field you did not is a privilege change you did not
    intend. At least one flag is required; an empty body would 200 having done
    nothing, which reads as success.

    Things that bite:
      * `--admin` is a real privilege boundary, not a label. A system admin reads
        and writes EVERY repo (and every repo's secrets) regardless of who owns
        them, and can mint machine tokens. It is not repo admin.
      * `--no-active` is the block switch: their token stops authenticating
        immediately. Their repos stay put (unlike `user rm`, which transfers them).
      * Flipping `--machine` does not create a token, and a machine account's
        token can never be read back — if you need one, `user add --machine`.
      * Revoking your own `--admin` (or `--active`) prompts first: this command is
        admin-only, so you cannot undo it yourself.
      * You cannot rename a user here; login is the address, not a field.
    """
    obj = ctx_obj(ctx)
    client = obj.client()

    body = _tri_state_body(admin=admin, active=active, machine=machine)
    if not body:
        raise ValidationError(
            f"nothing to update: pass at least one of --admin/--no-admin, --active/--no-active, "
            f"--machine/--no-machine. Every flag is tri-state, so omitting them all sends an "
            f"empty PATCH — the server would answer 200 having changed nothing. Current values: "
            f"`drone-cli user info {login}`."
        )

    _guard_self_lockout(client, login, admin=admin, active=active, yes=yes)

    with admin_scope(f"`user update {login}` (PATCH /api/users/{login})"):
        try:
            updated = client.patch(f"users/{login}", json=body)
        except NotFoundError as exc:
            raise NotFoundError(
                f"no Drone user '{login}' to update. Logins are SCM usernames and are "
                f"case-sensitive here; a user only exists in Drone once they have logged in (or "
                f"been created with `drone-cli user add`). List them: `drone-cli user ls`.",
                detail=getattr(exc, "detail", None),
            ) from exc

    out = _shape(updated or {})
    out["updated"] = sorted(body)

    # Read back what we asked for. The server is the authority on what it stored,
    # and reporting "admin: true" because we *sent* it would be inventing an
    # outcome -- the sibling `repo update` route is already known to drop
    # admin-gated fields and still answer 200 with the old values.
    if updated:
        drifted = [k for k, v in body.items() if bool(updated.get(k)) != bool(v)]
        if drifted:
            out["warning"] = (
                f"the server did not apply {', '.join(sorted(drifted))} — it answered 200 with the "
                f"old value(s). Verify with `drone-cli user info {login}`."
            )

    if admin is not None:
        out["note"] = (
            f"PRIVILEGE CHANGE: {login} {'now HAS' if admin else 'no longer has'} system admin. "
            f"A system admin reads and writes every repo and every repo secret on this server, "
            f"bypassing repo permissions entirely."
        )
    obj.emitter.emit(out, columns=_COLUMNS)


@app.command("rm")
def rm(
    ctx: typer.Context,
    login: str = typer.Argument(..., help="The login to delete."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Delete a user. ADMIN ONLY (403 otherwise). Wider blast radius than it looks.

    Deleting a user does not only remove a login: the server asynchronously
    transfers that user's repositories to another owner and fires a webhook. Any
    token they held (a machine account's especially) stops working immediately and
    is unrecoverable — there is no undo and no export.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    if not yes:
        typer.confirm(
            f"Delete Drone user {login}? Their token dies with them and their repos are "
            f"transferred away. This cannot be undone.",
            abort=True,
        )
    with admin_scope(f"`user rm {login}` (DELETE /api/users/{login})"):
        try:
            client.delete(f"users/{login}")
        except NotFoundError as exc:
            raise NotFoundError(
                f"no Drone user '{login}' to delete. List them: `drone-cli user ls`.",
                detail=getattr(exc, "detail", None),
            ) from exc
    obj.emitter.emit({"status": "deleted", "login": login})


# ---------------------------------------------------------------------------
# The one command here that is about YOU, not about the directory. It reads the
# singular `/api/user` path, so it needs no admin and never 403s for that reason.
# ---------------------------------------------------------------------------

def _feed_row(repo: dict) -> dict:
    """One repo, with its latest build hoisted into readable top-level fields.

    Passthrough + hoist (the `repo ls` pattern), not an allowlist: the nested
    `build` object stays so nothing is lost, and the columns pick the table view.
    Note `build.target` -- not `build.branch` -- is a build's branch, and it is
    hoisted to `last_build_branch` rather than `branch`, which on a repo object is
    already taken by the repo's DEFAULT branch. Those two differ exactly when the
    feed is interesting.
    """
    out = dict(repo)
    out["slug"] = repo.get("slug") or f"{repo.get('namespace', '')}/{repo.get('name', '')}"
    # A repo with no build yet comes back with `build` absent or null (not {}),
    # which NPEs naive hoisting. Emit explicit nulls: "never built" is a real,
    # useful answer in a fleet-health table, not a row to drop.
    build = repo.get("build") or {}
    out["last_build_status"] = build.get("status")
    out["last_build_number"] = build.get("number")
    out["last_build_event"] = build.get("event")
    out["last_build_branch"] = build.get("target")
    return out


@app.command("feed")
def feed(ctx: typer.Context) -> None:
    """Your build feed: every repo you can see, with its LATEST build. Not admin-only.

    Despite the endpoint being called `/api/user/builds`, it does not return
    builds. It returns **repo objects**, one per repo, each carrying only its most
    recent build — verified live. So this is a fleet-health snapshot, not a
    history: `last_build_number` 42 tells you nothing about build 41.

    Why it matters: **Drone has no cross-repo build search.** There is no
    "show me every failing build" endpoint anywhere in the API; every other build
    route is scoped to one repo. This single call is the only way to see the state
    of everything at once — to find the red repos, fan out from here:

        drone-cli user feed --fields slug,last_build_status
        drone-cli build ls --repo <the red one>

    Close cousin: `drone-cli repo ls --latest` returns the same shape from
    `/api/user/repos?latest=true`. That one lists repos and can include ones that
    have never built; this one is the activity feed. Same field names either way.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    # No `paginate`: like /api/user/repos, the feed handler ignores page/per_page
    # and returns the full array, so paging it would fetch page 1 forever.
    rows = client.get("user/builds") or []
    if not isinstance(rows, list):
        rows = [rows]
    out = [_feed_row(r) for r in rows if isinstance(r, dict)]
    obj.emitter.emit(out, columns=_FEED_COLUMNS)
