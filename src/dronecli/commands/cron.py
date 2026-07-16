"""`drone-cli cron` — scheduled builds, and the guard that makes them safe.

Two things about Drone's crons are true and neither is discoverable from the API:

1. **The expression is seconds-first** (six fields). A standard crontab line is
   accepted and silently means something else — see :mod:`dronecli.cronspec`.
   So every write here refuses a 5-field expression and every write prints the
   next fire times *before* creating anything. The server cannot help: `next` is
   computed only after the row is persisted, so by the time it could tell you the
   schedule is wrong, the wrong schedule exists.
2. **PATCH silently drops `name` and `expr`.** The server's update struct is
   `{branch, target, disabled}` only; anything else is decoded, ignored, and
   answered with a 200 and an unchanged object. `update` diffs the response and
   fails loudly rather than passing that lie on.
"""

from __future__ import annotations

from datetime import datetime, timezone

import typer

from .. import cronspec as C
from ..errors import NotFoundError, OpError, ValidationError
from ._shared import ctx_obj, need_repo

app = typer.Typer(no_args_is_help=True)

_COLUMNS = ["name", "expr", "branch", "disabled", "next_utc", "prev_utc"]

#: Drone evaluates schedules in the SERVER's timezone, which the API does not
#: expose anywhere. A stock container is UTC, so we preview in UTC and say so —
#: previewing in the operator's local zone would be a confident wrong answer on
#: every server whose TZ differs from the laptop's.
_TZ_NOTE = (
    "times are UTC. Drone evaluates crons in the SERVER's timezone (UTC in a stock "
    "container); if your server sets TZ, shift these by that offset."
)


def _iso(epoch) -> str | None:
    """Epoch seconds -> ISO, treating 0 as 'never', not as 1970."""
    if not epoch:
        return None
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()


def _decorate(cron: dict) -> dict:
    """Add the readable fields the API refuses to provide.

    `next`/`prev` are raw epoch ints, and `prev` is 0 on a cron that has never
    run — which renders as 1970 and reads as "it ran, in the Nixon era".
    """
    out = dict(cron)
    out["next_utc"] = _iso(cron.get("next"))
    out["prev_utc"] = _iso(cron.get("prev"))
    return out


def _preview(expr: str, n: int = 5) -> list[str]:
    """The next N fire times as ISO strings, or [] if we cannot parse the expr.

    Never raises: a preview failing must not stop `cron ls` from listing a cron
    the server already accepted (its syntax may be legal robfig we don't model).
    """
    try:
        return [d.isoformat() for d in C.next_fire_times(expr, n, now=datetime.now(timezone.utc))]
    except (ValidationError, OpError):
        return []


def _resolve_expr(expr: str | None, at: str | None, every: str | None, preset: str | None) -> str:
    """Turn exactly one of the four ways of saying "when" into a 6-field expr."""
    given = [("--expr", expr), ("--at", at), ("--every", every), ("--preset", preset)]
    chosen = [(flag, val) for flag, val in given if val]
    if not chosen:
        raise ValidationError(
            "say when: --expr '0 0 3 * * *' (6-field, seconds first), --at '3am daily', "
            f"--every 15m, or --preset <{'|'.join(sorted(C.PRESETS))}>."
        )
    if len(chosen) > 1:
        raise ValidationError(
            f"{' and '.join(f for f, _ in chosen)} are mutually exclusive — pick one."
        )

    flag, value = chosen[0]
    if flag == "--expr":
        # THE guard. Not a warning: a 5-field expr is 24x wrong and invisible
        # once created, and the caller has told us in the same breath what they
        # meant, so there is a correct answer to hand them.
        if C.looks_like_5_field(value):
            raise ValidationError(
                f"{value!r} is a 5-field crontab expression. Drone's cron is SECONDS-FIRST "
                f"(second minute hour dom month dow), so it would ACCEPT this and read it as "
                f"{C.misread_as(value)} — firing every hour, not on the schedule you wrote. "
                f"Use --expr '{C.to_6_field(value)}' (or --at/--every/--preset).",
                detail=C.explain_5_field(value),
            )
        C.parse(value)  # syntax check now, locally, with a useful message
        return " ".join(value.split())
    if flag == "--preset":
        got = C.PRESETS.get(value.strip().lower())
        if got is None:
            raise ValidationError(
                f"unknown preset {value!r}. Known: {', '.join(sorted(C.PRESETS))}."
            )
        return got
    if flag == "--every":
        return C.from_human(f"every {value.strip()}")
    return C.from_human(value)


def _default_branch(client, slug: str) -> str:
    """The repo's default branch, for when --branch is omitted.

    A cron with no branch would schedule builds of nothing, so this must resolve
    to something. `branch` is the field Drone's repo object carries; the fallback
    chain exists because that key is the one thing here not pinned by the spike.
    """
    repo = client.get(f"repos/{slug}") or {}
    branch = repo.get("branch") or repo.get("default_branch")
    if not branch:
        raise ValidationError(
            f"could not determine the default branch of {slug} — pass --branch explicitly."
        )
    return branch


@app.command("ls")
def ls(
    ctx: typer.Context,
    repo: str = typer.Option(None, "--repo", "-r", help="owner/name (or set a sticky context)."),
    disabled: bool = typer.Option(None, "--disabled/--enabled", help="Only disabled (or only enabled) crons."),
    preview: bool = typer.Option(False, "--preview", help="Also compute the next 5 fire times locally for each."),
) -> None:
    """List the repo's cron jobs.

    Listing crons needs **write** access to the repo — a 403 here means your
    token is read-only, NOT that the repo has no crons. The endpoint returns the
    whole array in one response; there is no pagination.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    slug = need_repo(obj, repo)

    rows = client.get(f"repos/{slug}/cron") or []
    if disabled is not None:
        rows = [c for c in rows if bool(c.get("disabled")) is disabled]
    out = [_decorate(c) for c in rows]
    if preview:
        for row in out:
            row["next_fire_times"] = _preview(row.get("expr") or "")
    obj.emitter.emit(out, columns=_COLUMNS)


@app.command("get")
def get(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Cron name as the SERVER stores it (it slugifies on create)."),
    repo: str = typer.Option(None, "--repo", "-r", help="owner/name."),
) -> None:
    """Show one cron, plus what its schedule actually means.

    `next_fire_times` is computed locally from `expr` — it is the only way to see
    more than one step ahead, since the server keeps a single `next`.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    slug = need_repo(obj, repo)

    try:
        cron = client.get(f"repos/{slug}/cron/{name}")
    except NotFoundError:
        raise NotFoundError(
            f"no cron named {name!r} in {slug}. Names are slugified on create "
            f"('Nightly Build' -> 'nightly-build'); list them with: drone-cli cron ls --repo {slug}"
        ) from None

    out = _decorate(cron)
    expr = out.get("expr") or ""
    out["next_fire_times"] = _preview(expr)
    out["timezone_note"] = _TZ_NOTE
    if C.looks_like_5_field(expr):
        # It already exists, so this is a diagnosis rather than a refusal.
        out["warning"] = (
            f"this cron's expr is 5-field: Drone reads it as {C.misread_as(expr)} and fires it "
            f"every hour. Fix: drone-cli cron update {name} --expr '{C.to_6_field(expr)}' "
            f"--recreate --repo {slug}"
        )
    obj.emitter.emit(out)


@app.command("next")
def next_(
    ctx: typer.Context,
    expr: str = typer.Option(None, "--expr", help="6-field expression: second minute hour dom month dow."),
    at: str = typer.Option(None, "--at", help="Plain English, e.g. '3am daily', 'every monday at 9am'."),
    every: str = typer.Option(None, "--every", help="An interval, e.g. 15m, 1h, 30s."),
    preset: str = typer.Option(None, "--preset", help=f"One of: {', '.join(sorted(C.PRESETS))}."),
    count: int = typer.Option(5, "--count", "-n", help="How many fire times to show."),
) -> None:
    """Preview a schedule WITHOUT creating anything. No server call.

    The API cannot do this at all: `next` only exists once a cron is persisted,
    and it is one step deep. Use this to check an expression means what you think
    before it becomes a job that quietly runs 24x too often.

        drone-cli cron next --expr '0 0 3 * * *'      # daily at 03:00
        drone-cli cron next --at '3am daily'
    """
    obj = ctx_obj(ctx)
    resolved = _resolve_expr(expr, at, every, preset)
    now = datetime.now(timezone.utc)
    obj.emitter.emit(
        {
            "expr": resolved,
            "means": C.describe(resolved),
            "next_fire_times": [d.isoformat() for d in C.next_fire_times(resolved, count, now=now)],
            "timezone_note": _TZ_NOTE,
        }
    )


@app.command("add")
def add(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Cron name. The server SLUGIFIES it ('Nightly Build' -> 'nightly-build')."),
    repo: str = typer.Option(None, "--repo", "-r", help="owner/name."),
    expr: str = typer.Option(None, "--expr", help="6-field expression: SECOND minute hour dom month dow. A 5-field crontab line is REFUSED."),
    at: str = typer.Option(None, "--at", help="Plain English, e.g. '3am daily', 'every monday at 9am'."),
    every: str = typer.Option(None, "--every", help="An interval, e.g. 15m, 1h."),
    preset: str = typer.Option(None, "--preset", help=f"One of: {', '.join(sorted(C.PRESETS))}."),
    branch: str = typer.Option(None, "--branch", "-b", help="Branch to build (default: the repo's default branch)."),
) -> None:
    """Create a cron job. Prints the next 5 fire times it will produce.

        drone-cli cron add nightly --at '3am daily'
        drone-cli cron add nightly --expr '0 0 3 * * *'      # identical, 6-field
        drone-cli cron add nightly --expr '0 3 * * *'        # REFUSED: 5-field

    Two things to know:
      * The expression is SECONDS-FIRST. `0 3 * * *` is not a syntax error to
        Drone — it means second=0 minute=3 hour=*, i.e. hourly at :03.
      * The cron's own `event` is forced to `push` server-side, but the BUILDS it
        creates carry `event=cron`. A pipeline gated on `event: [push]` will not
        run from a cron; gate on `cron` (or both).
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    slug = need_repo(obj, repo)
    resolved = _resolve_expr(expr, at, every, preset)
    target = branch or _default_branch(client, slug)

    # Preview BEFORE the write, always. This is the entire value of the command:
    # once the row exists, a wrong schedule is invisible and simply runs.
    fires = [d.isoformat() for d in C.next_fire_times(resolved, 5, now=datetime.now(timezone.utc))]
    obj.emitter.message(
        f"'{resolved}' ({C.describe(resolved)}) — next 5: " + ", ".join(fires) + f"\n{_TZ_NOTE}"
    )

    created = client.post(f"repos/{slug}/cron", json={"name": name, "expr": resolved, "branch": target})
    out = _decorate(created or {})
    out["next_fire_times"] = fires
    out["timezone_note"] = _TZ_NOTE
    server_name = out.get("name")
    if server_name and server_name != name:
        out["note"] = (
            f"the server slugified the name: {name!r} -> {server_name!r}. Address it by "
            f"{server_name!r} from now on."
        )
    obj.emitter.emit(out)


@app.command("update")
def update(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Existing cron name."),
    repo: str = typer.Option(None, "--repo", "-r", help="owner/name."),
    expr: str = typer.Option(None, "--expr", help="New 6-field expression. PATCH cannot do this — needs --recreate."),
    at: str = typer.Option(None, "--at", help="New schedule in plain English. Needs --recreate."),
    every: str = typer.Option(None, "--every", help="New interval. Needs --recreate."),
    preset: str = typer.Option(None, "--preset", help="New preset schedule. Needs --recreate."),
    branch: str = typer.Option(None, "--branch", "-b", help="New branch to build. PATCH supports this."),
    disabled: bool = typer.Option(None, "--disable/--enable", help="Pause or resume the cron. PATCH supports this."),
    rename: str = typer.Option(None, "--rename", help="New name. PATCH cannot do this — needs --recreate."),
    recreate: bool = typer.Option(False, "--recreate", help="Allow DELETE+POST to change expr/name. Resets id and prev history."),
) -> None:
    """Change a cron — honestly.

    **The server's PATCH only understands `{branch, target, disabled}`.** Send it
    a new `expr` or `name` and it decodes them, throws them away, and returns
    **200 with the object unchanged**. (It also ignores JSON decode errors
    outright, so a malformed body is a 200 no-op.) Every client that trusts that
    200 reports a schedule change that did not happen.

    So this command does two different things:

      * `--branch` / `--disable` / `--enable` -> a real PATCH, and we diff the
        response. If the server did not actually apply it, you get an error, not
        a green tick.
      * `--expr` / `--at` / `--every` / `--preset` / `--rename` -> **refused by
        default**, because the only way to do them is DELETE + POST. That is not
        an "update": it mints a new id, drops `prev` history, and — since the two
        calls are not atomic — leaves the repo with NO cron at all if the POST
        fails. Pass `--recreate` to say you accept that. Refusing to guess here
        is deliberate: silently destroying and rebuilding a schedule behind an
        innocuous verb is exactly the kind of surprise this CLI exists to avoid.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    slug = need_repo(obj, repo)

    wants_expr = any([expr, at, every, preset])
    if not wants_expr and not rename and branch is None and disabled is None:
        raise ValidationError(
            "nothing to change. Pass --branch, --disable/--enable, or "
            "--expr/--at/--every/--preset (with --recreate)."
        )

    new_expr = _resolve_expr(expr, at, every, preset) if wants_expr else None

    if (new_expr or rename) and not recreate:
        what = " and ".join(x for x in (["expr"] if new_expr else []) + (["name"] if rename else []))
        raise OpError(
            f"the server CANNOT patch a cron's {what}: its update struct is "
            f"{{branch, target, disabled}} only, and it silently discards the rest while "
            f"returning 200. The only way is DELETE + POST, which mints a new id, resets "
            f"`prev` history, and is not atomic (a failed POST leaves no cron at all). "
            f"Re-run with --recreate if that is acceptable.",
            detail={"repo": slug, "cron": name, "requested": {"expr": new_expr, "name": rename}},
        )

    current = client.get(f"repos/{slug}/cron/{name}")

    if new_expr or rename:
        final_name = rename or current.get("name") or name
        final_expr = new_expr or current.get("expr")
        final_branch = branch if branch is not None else current.get("branch")
        fires = [d.isoformat() for d in C.next_fire_times(final_expr, 5, now=datetime.now(timezone.utc))]
        obj.emitter.message(
            f"'{final_expr}' ({C.describe(final_expr)}) — next 5: " + ", ".join(fires)
        )
        client.delete(f"repos/{slug}/cron/{name}")
        created = client.post(
            f"repos/{slug}/cron",
            json={"name": final_name, "expr": final_expr, "branch": final_branch},
        )
        out = _decorate(created or {})
        out["next_fire_times"] = fires
        out["timezone_note"] = _TZ_NOTE
        out["recreated"] = True
        out["note"] = (
            f"cron {name!r} was DELETED and recreated as {out.get('name')!r} — the server cannot "
            f"patch expr/name. New id {out.get('id')} (was {current.get('id')}); `prev` run "
            f"history is gone."
        )
        if disabled is not None and bool(out.get("disabled")) is not disabled:
            # A recreated cron comes back enabled; carry the flag over.
            out = _decorate(client.patch(f"repos/{slug}/cron/{out.get('name')}", json={"disabled": disabled}))
            out["recreated"] = True
        obj.emitter.emit(out)
        return

    body: dict = {}
    if branch is not None:
        body["branch"] = branch
    if disabled is not None:
        body["disabled"] = disabled

    res = client.patch(f"repos/{slug}/cron/{name}", json=body) or {}

    # Diff request against response. The server answers 200 for changes it never
    # made, so "no error" proves nothing here -- only the returned object does.
    dropped = {k: v for k, v in body.items() if res.get(k) != v}
    if dropped:
        raise OpError(
            f"the server returned 200 but did NOT apply {sorted(dropped)} to cron {name!r} in "
            f"{slug}. Drone's cron PATCH silently discards fields it does not accept and never "
            f"reports it — this is that, caught by diffing the response. Nothing was changed.",
            detail={"requested": body, "server_returned": {k: res.get(k) for k in body}},
        )

    out = _decorate(res)
    out["next_fire_times"] = _preview(out.get("expr") or "")
    out["timezone_note"] = _TZ_NOTE
    obj.emitter.emit(out)


@app.command("exec")
def exec_(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Cron name to run right now."),
    repo: str = typer.Option(None, "--repo", "-r", help="owner/name."),
) -> None:
    """Run a cron NOW, off-schedule, and report the build it created.

    Builds the current HEAD of the cron's branch, with `event=cron` — the same
    shape the schedule would have produced, so this is how you test a cron
    without waiting for 03:00.

    The response carries the created build, which the official drone-go client
    literally throws away (`c.post(uri, nil, nil)`); the build number is right
    here in `number`.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    slug = need_repo(obj, repo)

    build = client.post(f"repos/{slug}/cron/{name}")
    if not isinstance(build, dict) or not build.get("number"):
        # Some servers answer with an empty body. Say so rather than inventing a
        # number: an agent that gets a wrong number waits on someone else's build.
        obj.emitter.emit(
            {
                "status": "triggered",
                "repo": slug,
                "cron": name,
                "number": None,
                "note": (
                    "the server did not return a build object, so the build number is unknown. "
                    f"Find it with: drone-cli build ls --repo {slug} --event cron"
                ),
            }
        )
        return

    out = dict(build)
    out["cron"] = name
    out["note"] = (
        f"cron {name!r} triggered build #{build.get('number')} (event=cron) on "
        f"{build.get('target') or 'the cron branch'}. Wait for it: "
        f"drone-cli wait {build.get('number')} --repo {slug}"
    )
    obj.emitter.emit(out)


@app.command("rm")
def rm(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Cron name to delete."),
    repo: str = typer.Option(None, "--repo", "-r", help="owner/name."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm. Required when not on a TTY."),
) -> None:
    """Delete a cron job. Irreversible — recreating it resets id and run history.

    To pause one instead, `cron update NAME --disable` keeps the row.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    slug = need_repo(obj, repo)

    if not yes:
        if not obj.interactive:
            raise OpError(
                f"refusing to delete cron {name!r} from {slug} without confirmation. "
                f"Pass --yes. (To pause it instead: drone-cli cron update {name} --disable "
                f"--repo {slug}.)"
            )
        typer.confirm(f"Delete cron {name!r} from {slug}?", abort=True, err=True)

    client.delete(f"repos/{slug}/cron/{name}")
    obj.emitter.emit({"status": "deleted", "repo": slug, "cron": name})
