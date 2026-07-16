"""`drone-cli repo` — the repositories Drone knows about.

Two facts shape every command here:

* **Drone does not own repositories, it mirrors them.** A repo cannot exist in
  Drone until `repo sync` pulls it from the git provider, and it does nothing
  until `repo enable` activates it. "404" on a brand-new repo is the normal,
  expected first answer — not a bug.
* **`repo update` can lie.** The server silently drops admin-gated fields and
  still answers 200 with the old values, so every write here is verified against
  what came back rather than trusted.
"""

from __future__ import annotations

import typer

from ..errors import ApiError, OpError, ValidationError
from ._shared import ctx_obj, need_repo

app = typer.Typer(no_args_is_help=True)

#: The non-interactive way to say yes to `--trusted`. Long and unlovely on
#: purpose: it has to be impossible to type by accident, and impossible to read
#: in a script's diff without understanding what is being granted.
TRUSTED_ACK_FLAG = "--i-understand-this-grants-privileged-containers"

TRUSTED_WARNING = (
    "--trusted lets every pipeline in this repo run PRIVILEGED containers and mount the host "
    "filesystem. That is root on the runner for anyone who can push a .drone.yml — including, "
    "on a repo that builds pull requests, anyone who can open one. It is a privilege "
    "escalation, not a build tweak."
)

_COLUMNS = ["slug", "active", "default_branch", "visibility"]

#: The server accepts anything here. `handler/api/repos/update.go`'s
#: `govalidator.IsIn` check is commented out, so `{"visibility":"pubic"}` is
#: stored and returned 200 — a typo becomes a permanent, invisible setting.
#: Validating client-side is the only place it can be caught at all.
_VISIBILITIES = ("public", "private", "internal")

#: Fields the update handler gates on `user.Admin` (SYSTEM admin, not repo
#: admin) and drops in silence otherwise. Only used to explain a detected drop —
#: the drop detection itself diffs every field we sent, so it stays correct even
#: if a given server gates a different set.
_ADMIN_GATED = ("timeout", "trusted", "throttle", "counter")


def _decorate(repo: dict, links: bool) -> dict:
    """Flatten the bits of a repo an agent actually reads into top-level fields."""
    out = dict(repo)
    build = repo.get("build") or {}
    if build:
        # `?latest=true` hangs the whole build object off the repo. Hoist the two
        # fields that make a fleet-health table readable; the full object stays.
        out["last_build_status"] = build.get("status")
        out["last_build_number"] = build.get("number")
    if links:
        out["repo_url"] = repo.get("link")
    return out


def _slug_of(repo: dict) -> str:
    return repo.get("slug") or f"{repo.get('namespace', '')}/{repo.get('name', '')}"


def _list_repos(client, *, latest: bool) -> list[dict]:
    """Every repo the caller can see.

    `GET /api/user/repos` ignores `page`/`per_page` and returns the **whole**
    array, so the generic short-page terminator never fires for anyone with 100+
    repos: page 2 is page 1 again, forever. Stopping on the first slug we have
    already seen keeps that from becoming an infinite loop today, and stays
    correct if the endpoint ever grows real paging.
    """
    seen: set[str] = set()
    rows: list[dict] = []
    params = {"latest": "true"} if latest else None
    for r in client.paginate("user/repos", params=params):
        slug = _slug_of(r)
        if slug in seen:
            break
        seen.add(slug)
        rows.append(r)
    return rows


@app.command("ls")
def ls(
    ctx: typer.Context,
    all_repos: bool = typer.Option(
        False, "--all", help="Include repos that are NOT enabled in Drone (default: active only)."
    ),
    latest: bool = typer.Option(
        False, "--latest", help="Attach each repo's latest build — a whole fleet's health in ONE request."
    ),
    namespace: str = typer.Option(None, "--namespace", help="Only repos in this org/owner."),
    search: str = typer.Option(None, "--search", "-q", help="Substring match on the slug."),
    links: bool = typer.Option(False, "--links", help="Include the repo's web URL (repo_url)."),
) -> None:
    """List repositories, as Drone sees them.

    Default is **active repos only** — the ones Drone actually builds. `--all`
    adds every repo it merely knows about from the last sync, which on a big org
    is mostly noise.

        drone-cli repo ls                 # what am I building?
        drone-cli repo ls --latest        # ...and is any of it red?

    `--latest` uses the undocumented `?latest=true`, which returns every repo
    WITH its last build attached. That is one request for the whole fleet rather
    than one per repo — use it instead of looping `build ls`.

    A repo you just created in the git provider will not appear at all until
    `drone-cli repo sync` runs.
    """
    obj = ctx_obj(ctx)
    client = obj.client()

    rows = _list_repos(client, latest=latest)
    if not all_repos:
        rows = [r for r in rows if r.get("active")]
    if namespace:
        rows = [r for r in rows if (r.get("namespace") or "") == namespace]
    if search:
        needle = search.lower()
        rows = [r for r in rows if needle in _slug_of(r).lower()]
    rows.sort(key=_slug_of)

    columns = list(_COLUMNS)
    if latest:
        columns.append("last_build_status")
    if links:
        columns.append("repo_url")
    obj.emitter.emit([_decorate(r, links) for r in rows], columns=columns)


@app.command("sync")
def sync(
    ctx: typer.Context,
    links: bool = typer.Option(False, "--links", help="Include each repo's web URL (repo_url)."),
) -> None:
    """Re-read the repository list from the git provider.

    **A brand-new repo is invisible to Drone until this runs.** Drone does not
    discover repos on its own and the git provider does not push the list to it,
    so `repo info`/`repo enable` on a repo created five minutes ago return a
    baffling 404 until you sync. If a repo "does not exist", sync first, then
    look again.

    This is also the command that proves the server's SCM link works: it is the
    one call that must use the git token, so it is where a dead SCM link shows
    up (as an auth error naming exactly that).
    """
    obj = ctx_obj(ctx)
    client = obj.client()

    # Synchronous by default: the refreshed list IS the response body, so there
    # is nothing to poll and nothing to wait for.
    rows = client.post("user/repos")
    if not isinstance(rows, list):
        rows = [rows] if rows else []
    columns = list(_COLUMNS) + (["repo_url"] if links else [])
    obj.emitter.emit([_decorate(r, links) for r in rows], columns=columns)


@app.command("info")
def info(
    ctx: typer.Context,
    slug_arg: str = typer.Argument(None, metavar="[REPO]", help="owner/name, positionally."),
    repo: str = typer.Option(None, "--repo", "-r", help="owner/name (or set a sticky context)."),
    links: bool = typer.Option(False, "--links", help="Include the repo's web URL (repo_url)."),
) -> None:
    """Show one repository, including your permissions on it.

    This is the only repo endpoint that returns `permissions {read,write,admin}`
    — use it to find out whether a write will be allowed before attempting it.

    A 404 here means one of three different things: the repo does not exist, it
    exists in the git provider but Drone has not synced it (`drone-cli repo
    sync`), or you cannot see it. It does NOT mean the repo is merely disabled —
    a disabled repo still answers 200 with `active: false`.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    slug = need_repo(obj, slug_arg or repo)
    obj.emitter.emit(_decorate(client.get(f"repos/{slug}"), links))


@app.command("enable")
def enable(
    ctx: typer.Context,
    slug_arg: str = typer.Argument(None, metavar="[REPO]", help="owner/name, positionally."),
    repo: str = typer.Option(None, "--repo", "-r", help="owner/name (or set a sticky context)."),
    sync_first: bool = typer.Option(
        False, "--sync", help="Sync from the git provider first — needed for a repo Drone has never seen."
    ),
) -> None:
    """Activate a repository: register its webhook and start building it.

    **This silently makes you the repo's owner.** Enabling sets `repo.UserID` to
    YOU, which moves every webhook registration, every clone and every
    `.drone.yml` fetch onto YOUR git token — not the token of whoever set the
    repo up before. Two consequences worth knowing before you run it:

      * builds break the day your git token is revoked or you leave;
      * re-enabling an already-enabled repo re-chowns it to you, so this is a
        quiet way to take a colleague's repo hostage by accident.

    Other things that surprise people exactly once:

      * 404 → Drone has never synced this repo. Use `--sync`.
      * 402 → the repo limit on a licensed server is reached; nothing is wrong
        with your request, the server will not activate one more repo until
        another is disabled (see `/varz` for the live count).
      * Defaults applied on activation: `config_path=.drone.yml`, `timeout=60`.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    slug = need_repo(obj, slug_arg or repo)

    if sync_first:
        client.post("user/repos")

    try:
        out = client.post(f"repos/{slug}")
    except ApiError as exc:
        # 402 is a licensing state, not a malformed request. Left as a bare
        # "payment required" it reads like a billing bug in the CLI.
        if getattr(exc, "status", None) == 402:
            raise ApiError(
                f"cannot enable {slug}: the server's active-repo limit is reached (HTTP 402). "
                f"This is a license limit, not a problem with the request — disable a repo "
                f"(`drone-cli repo disable <slug>`) or raise the limit. Live counts: GET /varz.",
                status=402,
                detail=exc.detail,
            ) from exc
        raise

    res = _decorate(out or {"slug": slug, "active": True}, False)
    res["note"] = (
        f"{slug} is active and now OWNED BY YOU — its webhook, clones and .drone.yml fetches "
        f"use your git token from here on. Trigger a build: drone-cli build run --repo {slug}"
    )
    obj.emitter.emit(res)


@app.command("disable")
def disable(
    ctx: typer.Context,
    slug_arg: str = typer.Argument(None, metavar="[REPO]", help="owner/name, positionally."),
    repo: str = typer.Option(None, "--repo", "-r", help="owner/name (or set a sticky context)."),
) -> None:
    """Deactivate a repository: stop building it.

    Sets `active: false` and keeps everything else — settings, secrets and build
    history all survive, and `repo enable` brings it back as it was.

    **It does NOT remove the webhook from the git provider**, contrary to the
    docs and to common belief; the handler has no hook service at all. The git
    provider keeps POSTing to Drone and Drone keeps ignoring it. If you need the
    hook gone, delete it in the git provider.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    slug = need_repo(obj, slug_arg or repo)
    client.delete(f"repos/{slug}")
    obj.emitter.emit(
        {
            "status": "disabled",
            "repo": slug,
            "active": False,
            "note": (
                "the git provider's webhook is NOT removed by this — Drone will simply ignore "
                "deliveries. Settings, secrets and history are kept; `repo enable` restores them."
            ),
        }
    )


@app.command("update")
def update(
    ctx: typer.Context,
    slug_arg: str = typer.Argument(None, metavar="[REPO]", help="owner/name, positionally."),
    repo: str = typer.Option(None, "--repo", "-r", help="owner/name (or set a sticky context)."),
    timeout: int = typer.Option(None, "--timeout", help="Build timeout in MINUTES (not seconds). SYSTEM-ADMIN only."),
    protected: bool = typer.Option(
        None, "--protected/--no-protected", help="Require approval before a build runs."
    ),
    trusted: bool = typer.Option(
        None, "--trusted/--no-trusted", help="DANGEROUS: lets pipelines run privileged containers. SYSTEM-ADMIN only."
    ),
    visibility: str = typer.Option(
        None, "--visibility", help="public | private | internal. Checked here — the server accepts typos."
    ),
    config_path: str = typer.Option(
        None, "--config-path", help="Pipeline file to read, e.g. .drone.yml. Wrong value = every build errors."
    ),
    trusted_ack: bool = typer.Option(
        False,
        TRUSTED_ACK_FLAG,
        help=(
            "Confirm --trusted without a prompt. Required to grant it off a TTY; it is the "
            "only way to say yes non-interactively, and it must be typed deliberately."
        ),
    ),
) -> None:
    """Change a repository's settings — and verify the change actually landed.

        drone-cli repo update --repo octocat/hello-world --protected --timeout 90

    **Why this command is not a thin PATCH.** `--timeout` and `--trusted` are
    gated on *system* admin, not repo admin. For anyone else the server accepts
    the request, answers **200 with the old values**, and warns nobody: the field
    is dropped in silence. Believing that 200 is how an agent concludes "the
    timeout is now 90" when it is still 60. So every field sent here is diffed
    against the object that comes back, and a dropped one is a hard failure
    (exit 7) that names the field.

    `--trusted` grants pipelines privileged containers and host mounts — it is a
    privilege escalation for anyone who can push a `.drone.yml`, not a build
    tweak. So it is gated: you confirm at a prompt, or you pass
    `--i-understand-this-grants-privileged-containers` when there is no TTY to
    prompt at. It is never granted silently. (`--no-trusted` REVOKES the grant and
    needs no gate — taking privilege away is not the dangerous direction.)

    `--visibility` is validated locally because the server's own check is
    commented out: a typo would be stored and returned 200 forever.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    slug = need_repo(obj, slug_arg or repo)

    # Before the PATCH, and before anything else can fail: a privilege grant that
    # slipped through on a typo is not undone by erroring afterwards.
    if trusted is True:
        _gate_trusted(obj, slug, trusted_ack)

    body: dict = {}
    if timeout is not None:
        body["timeout"] = timeout
    if protected is not None:
        body["protected"] = protected
    if trusted is not None:
        body["trusted"] = trusted
    if visibility is not None:
        if visibility not in _VISIBILITIES:
            raise ValidationError(
                f"--visibility must be one of {', '.join(_VISIBILITIES)}, got {visibility!r}. "
                f"(The server would ACCEPT that typo and store it — it does not validate this field.)"
            )
        body["visibility"] = visibility
    if config_path is not None:
        body["config_path"] = config_path

    if not body:
        raise ValidationError(
            "nothing to update. Pass at least one of --timeout, --protected/--no-protected, "
            "--trusted/--no-trusted, --visibility, --config-path."
        )

    updated = client.patch(f"repos/{slug}", json=body) or {}
    dropped = _dropped_fields(body, updated)
    out = _decorate(updated, False)
    out["applied"] = sorted(body)

    if dropped:
        raise ValidationError(_drop_message(slug, dropped, _is_system_admin(client)), detail={
            "repo": slug,
            "requested": body,
            "dropped": dropped,
            "effective": {k: updated.get(k) for k in body},
        })
    obj.emitter.emit(out)


def _gate_trusted(obj, slug: str, acknowledged: bool) -> None:
    """Never grant `trusted` silently — confirm it, or demand the ack flag.

    Mirrors `cron rm`'s shape (prompt when there is a human, refuse otherwise)
    rather than inventing a second convention. `--yes` is deliberately NOT the
    escape hatch: `-y` is muscle memory and gets pasted into scripts to make
    prompts go away, so reusing it would mean the guard is bypassed by everyone
    who has ever been annoyed by a different prompt. The flag has to name what it
    grants.

    Note this stacks with the drop-guard rather than replacing it: consent is not
    permission. `trusted` is admin-gated server-side and dropped in silence for
    non-admins, so a confirmed grant that never landed is still caught after the
    PATCH and reported as a failure.
    """
    if acknowledged:
        return
    if not obj.interactive:
        raise OpError(
            f"refusing to grant --trusted on {slug} without confirmation. {TRUSTED_WARNING} "
            f"There is no TTY to confirm at, so say so explicitly: re-run with "
            f"{TRUSTED_ACK_FLAG}."
        )
    typer.confirm(
        f"{TRUSTED_WARNING}\nGrant trusted to {slug}?",
        abort=True,
        err=True,  # stdout is the machine channel; a prompt there corrupts the payload
    )


def _dropped_fields(sent: dict, got: dict) -> list[str]:
    """Which of the fields we sent did the server not apply?

    The comparison has to normalise, not just `!=`: every field on the repo
    object is `omitempty`, so an applied `protected: false` comes back **absent**
    rather than false, and a naive diff would report every single false/0/"" as
    dropped — crying wolf on exactly the command whose value is that it doesn't.
    """
    out = []
    for key, want in sent.items():
        have = got.get(key)
        if isinstance(want, bool):
            same = bool(have) == want
        elif isinstance(want, int):
            same = int(have or 0) == want
        else:
            same = (have or "") == want
        if not same:
            out.append(key)
    return sorted(out)


def _is_system_admin(client) -> bool | None:
    """Best-effort: are we a Drone system admin? None when we could not tell.

    Only consulted once a drop is already detected — it buys a better error
    message, and must never turn a working command into a failing one.
    """
    try:
        return bool((client.get("user") or {}).get("admin"))
    except Exception:
        return None


def _drop_message(slug: str, dropped: list[str], is_admin: bool | None) -> str:
    fields = ", ".join(f"--{f.replace('_', '-')}" for f in dropped)
    admin_gated = [f for f in dropped if f in _ADMIN_GATED]
    msg = (
        f"the server accepted the request with HTTP 200 but did NOT apply {fields} on {slug} — "
        f"the values came back unchanged. The change did not happen; do not treat this as success."
    )
    if admin_gated and is_admin is False:
        msg += (
            f" {', '.join(admin_gated)} require Drone SYSTEM admin (not repo admin), and your "
            f"user is not one — the server drops those fields silently rather than refusing them."
        )
    elif admin_gated:
        msg += (
            f" {', '.join(admin_gated)} are gated on Drone SYSTEM admin (not repo admin); the "
            f"server drops them silently for everyone else."
        )
    return msg


@app.command("repair")
def repair(
    ctx: typer.Context,
    slug_arg: str = typer.Argument(None, metavar="[REPO]", help="owner/name, positionally."),
    repo: str = typer.Option(None, "--repo", "-r", help="owner/name (or set a sticky context)."),
) -> None:
    """Re-create the git provider's webhook and re-sync the repo's metadata.

    The fix for "pushes stopped triggering builds": the webhook is gone, or it
    points at a hostname that no longer resolves from the git provider.

    Two things to know before running it:

      * **repair APPENDS a hook, it does not replace the stale one.** The old,
        broken hook stays registered in the git provider. Repair a URL problem
        three times and the repo has three hooks, two of them dead; delete the
        stale ones by hand in the provider's UI.
      * It acts as the repo's OWNER, not as you — if the owner's git token is
        dead, repair fails no matter who runs it. Fix that with `repo chown`
        first.

    The endpoint returns nothing, so the repo is re-read afterwards and reported:
    a bare "success" here would prove nothing at all.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    slug = need_repo(obj, slug_arg or repo)

    client.post(f"repos/{slug}/repair")
    out = _decorate(client.get(f"repos/{slug}"), False)
    out["note"] = (
        "webhook re-registered. NOTE it was APPENDED — any previous (broken) hook is still "
        "registered in the git provider and must be deleted there by hand. Verify with a push, "
        f"or trigger directly: drone-cli build run --repo {slug}"
    )
    obj.emitter.emit(out)


@app.command("chown")
def chown(
    ctx: typer.Context,
    slug_arg: str = typer.Argument(None, metavar="[REPO]", help="owner/name, positionally."),
    repo: str = typer.Option(None, "--repo", "-r", help="owner/name (or set a sticky context)."),
) -> None:
    """Take ownership of a repository — always to YOURSELF.

    There is no target parameter: the API can only chown to the calling user. To
    hand a repo to someone else, they must run this themselves.

    What ownership means, and why you would want it: the owner's git token is
    what Drone uses to clone, to fetch `.drone.yml` and to manage the webhook.
    When the previous owner's token expires or they leave, every build for the
    repo starts failing with SCM errors that name nobody. Chowning to a live user
    fixes it — then `repo repair` to re-register the hook under the new owner.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    slug = need_repo(obj, slug_arg or repo)

    out = client.post(f"repos/{slug}/chown") or client.get(f"repos/{slug}")
    res = _decorate(out, False)
    res["note"] = (
        f"you now own {slug}; its clones, config fetches and webhook use YOUR git token. "
        f"If the hook was registered under the old owner, follow up with: "
        f"drone-cli repo repair --repo {slug}"
    )
    obj.emitter.emit(res)
