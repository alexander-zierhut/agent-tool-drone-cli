"""`drone-cli build` — and the two top-level shortcuts, `wait` and `promote`.

The design premise: **an agent knows its commit, not a build number.** Build
numbers are racy — two people pushing at once means "the latest build" may be
someone else's, and waiting on it reports their failure as yours.
"""

from __future__ import annotations

import time

import typer
from agentcli.errors import NotFoundError, OpError

from .. import builds as B
from ..errors import BuildNotFinished, CommitNotBuilt
from ._shared import ctx_obj, need_commit, need_repo

app = typer.Typer(no_args_is_help=True)

_COLUMNS = ["number", "status", "event", "target", "after", "author_login", "duration"]

# `wait --exit-code` opts into this band. It is deliberately far away from the
# error codes (1-10): "the build failed" is not "the CLI failed", and an agent
# must be able to tell them apart. Without --exit-code, observing a red build is
# still a successful run of this tool -> exit 0, outcome in the JSON.
EXIT_BUILD_FAILED = 20
EXIT_BUILD_BLOCKED = 24


def _decorate(obj, repo: dict | None, build: dict, links: bool) -> dict:
    """Add derived fields the API refuses to provide."""
    out = dict(build)
    out["duration"] = B.duration_seconds(build)
    out["queued_seconds"] = B.queue_seconds(build)
    if links and repo:
        url = obj.config.commit_url(repo.get("link") or "", build.get("after") or "")
        if url:
            out["commit_url"] = url
        out["repo_url"] = repo.get("link")
    return out


def _fetch_builds(client, slug: str, limit: int = 100) -> list[dict]:
    return list(client.paginate(f"repos/{slug}/builds", limit=limit))


def _find_for_commit(client, slug: str, sha: str, *, event: str | None, target: str | None,
                     pages: int = 200) -> list[dict]:
    """All builds for a commit.

    Drone has no server-side commit filter, so this is a client-side scan over
    the newest N builds. That is fine for the intended use (a commit you just
    pushed is near the top) and bounded so a busy repo can't spin forever.
    """
    all_builds = _fetch_builds(client, slug, limit=pages)
    return B.select_for_commit(all_builds, sha, event=event, target=target)


@app.command("ls")
def ls(
    ctx: typer.Context,
    repo: str = typer.Option(None, "--repo", "-r", help="owner/name (or set a sticky context)."),
    commit: str = typer.Option(None, "--commit", "-c", help="Only builds for this commit (accepts HEAD)."),
    branch: str = typer.Option(None, "--branch", "-b", help="Filter by target branch."),
    event: str = typer.Option(None, "--event", help="push | pull_request | tag | promote | rollback | cron | custom."),
    status: str = typer.Option(None, "--status", help="Filter by status, e.g. failure."),
    limit: int = typer.Option(25, "--limit", "-n", help="Max builds (0 = as many as we can page)."),
    links: bool = typer.Option(False, "--links", help="Include derived commit/repo web URLs."),
) -> None:
    """List builds, newest first."""
    obj = ctx_obj(ctx)
    client = obj.client()
    slug = need_repo(obj, repo)

    rows = _fetch_builds(client, slug, limit=limit or 200)
    if commit:
        rows = B.select_for_commit(rows, need_commit(commit))
    if branch:
        rows = [b for b in rows if (b.get("target") or "") == branch]
    if event:
        rows = [b for b in rows if (b.get("event") or "") == event]
    if status:
        rows = [b for b in rows if (b.get("status") or "") == status]
    if limit:
        rows = rows[:limit]

    repo_obj = client.get(f"repos/{slug}") if links else None
    obj.emitter.emit([_decorate(obj, repo_obj, b, links) for b in rows], columns=_COLUMNS)


@app.command("info")
def info(
    ctx: typer.Context,
    number: int = typer.Argument(None, help="Build NUMBER (not id). Omit and use --commit instead."),
    repo: str = typer.Option(None, "--repo", "-r", help="owner/name."),
    commit: str = typer.Option(None, "--commit", "-c", help="Address by commit instead (accepts HEAD)."),
    event: str = typer.Option(None, "--event", help="With --commit: which build to pick. Default: newest."),
    links: bool = typer.Option(False, "--links", help="Include derived commit/repo web URLs."),
) -> None:
    """Show one build, including its stages and steps.

    One GET returns the whole tree — stages[] with their steps[] embedded.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    slug = need_repo(obj, repo)

    if number is None:
        sha = need_commit(commit)
        found = _find_for_commit(client, slug, sha, event=event, target=None)
        if not found:
            raise CommitNotBuilt(f"no build found for commit {sha[:10]} in {slug}.")
        number = found[0]["number"]

    build = client.get(f"repos/{slug}/builds/{number}")
    repo_obj = client.get(f"repos/{slug}") if links else None
    out = _decorate(obj, repo_obj, build, links)
    out["failed_steps"] = B.failed_steps(build)
    obj.emitter.emit(out)


@app.command("run")
def run(
    ctx: typer.Context,
    repo: str = typer.Option(None, "--repo", "-r", help="owner/name."),
    branch: str = typer.Option(None, "--branch", "-b", help="Branch to build (default: the repo's default)."),
    commit: str = typer.Option(None, "--commit", "-c", help="Specific commit to build."),
    param: list[str] = typer.Option(None, "--param", "-P", help="KEY=VALUE build parameter (repeatable)."),
) -> None:
    """Trigger a build.

    NOTE: API-triggered builds have event=`custom`, not `push`. If your pipeline
    has `trigger: event: [push]`, it will not run — that is a pipeline config
    issue, not a CLI bug, and it surprises everyone exactly once.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    slug = need_repo(obj, repo)

    params: dict = {}
    if branch:
        params["branch"] = branch
    if commit:
        params["commit"] = need_commit(commit)
    for p in param or []:
        if "=" not in p:
            raise OpError(f"--param must be KEY=VALUE, got {p!r}")
        k, v = p.split("=", 1)
        params[k] = v

    build = client.post(f"repos/{slug}/builds", params=params)
    out = _decorate(obj, None, build, False)
    out["note"] = "API-triggered builds have event='custom'. A pipeline gated on `event: [push]` will skip."
    obj.emitter.emit(out)


@app.command("restart")
def restart(
    ctx: typer.Context,
    number: int = typer.Argument(..., help="Build NUMBER to restart."),
    repo: str = typer.Option(None, "--repo", "-r", help="owner/name."),
) -> None:
    """Restart a build. Creates a NEW build number — it does not resume the old one."""
    obj = ctx_obj(ctx)
    client = obj.client()
    slug = need_repo(obj, repo)
    build = client.post(f"repos/{slug}/builds/{number}")
    out = _decorate(obj, None, build, False)
    out["note"] = (
        f"restart of #{number} created build #{build.get('number')}. Parameters from the "
        f"PREVIOUS build take precedence over any you pass."
    )
    obj.emitter.emit(out)


@app.command("cancel")
def cancel(
    ctx: typer.Context,
    number: int = typer.Argument(..., help="Build NUMBER to cancel."),
    repo: str = typer.Option(None, "--repo", "-r", help="owner/name."),
) -> None:
    """Cancel a running build."""
    obj = ctx_obj(ctx)
    client = obj.client()
    slug = need_repo(obj, repo)
    client.delete(f"repos/{slug}/builds/{number}")
    obj.emitter.emit({"status": "cancelled", "repo": slug, "number": number})


@app.command("approve")
def approve(
    ctx: typer.Context,
    number: int = typer.Argument(..., help="Blocked build NUMBER."),
    stage: int = typer.Option(..., "--stage", help="Stage number to approve (1-based)."),
    repo: str = typer.Option(None, "--repo", "-r", help="owner/name."),
) -> None:
    """Approve a blocked stage. Requires admin on most servers."""
    obj = ctx_obj(ctx)
    client = obj.client()
    slug = need_repo(obj, repo)
    res = client.post(f"repos/{slug}/builds/{number}/approve/{stage}")
    obj.emitter.emit(res or {"status": "approved", "number": number, "stage": stage})


@app.command("decline")
def decline(
    ctx: typer.Context,
    number: int = typer.Argument(..., help="Blocked build NUMBER."),
    stage: int = typer.Option(..., "--stage", help="Stage number to decline (1-based)."),
    repo: str = typer.Option(None, "--repo", "-r", help="owner/name."),
) -> None:
    """Decline a blocked stage."""
    obj = ctx_obj(ctx)
    client = obj.client()
    slug = need_repo(obj, repo)
    res = client.post(f"repos/{slug}/builds/{number}/decline/{stage}")
    obj.emitter.emit(res or {"status": "declined", "number": number, "stage": stage})


# ---------------------------------------------------------------------------
# The two that live at the top level, because they are what the tool is for.
# ---------------------------------------------------------------------------


def wait(
    ctx: typer.Context,
    number: int = typer.Argument(None, help="Build NUMBER. Prefer --commit."),
    repo: str = typer.Option(None, "--repo", "-r", help="owner/name."),
    commit: str = typer.Option(None, "--commit", "-c", help="Wait for THIS commit's build. `HEAD` reads the local checkout."),
    event: str = typer.Option("push", "--event", help="With --commit: which build to wait for. Default: push."),
    timeout: str = typer.Option("30m", "--timeout", help="Give up after this long, e.g. 90s, 30m, 2h."),
    appear_timeout: str = typer.Option("2m", "--appear-timeout", help="How long to wait for the build to EXIST at all."),
    interval: float = typer.Option(5.0, "--interval", help="Seconds between polls."),
    exit_code: bool = typer.Option(False, "--exit-code", help="Exit non-zero when the build did not succeed (20-29)."),
    links: bool = typer.Option(False, "--links", help="Include derived commit/repo web URLs."),
) -> None:
    """Wait for a build to finish. Address it by COMMIT, not number.

    Why commit: build numbers are racy. If a colleague pushes while you do, "the
    latest build" may be theirs, and you would report their failure as yours. You
    already know your SHA.

        drone-cli wait --commit HEAD                  # did my push pass?
        drone-cli wait --commit HEAD --exit-code && ./deploy.sh

    Three outcomes an agent must distinguish, and they are NOT the same:
      * finished       -> the outcome is in the JSON (`status`)
      * blocked        -> a human must approve; the command to do it is included
      * never appeared -> exit 10. The webhook may be broken, or the repo not
                          enabled. That is not "the tests failed".
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    slug = need_repo(obj, repo)

    deadline = time.monotonic() + _duration(timeout)
    appear_deadline = time.monotonic() + _duration(appear_timeout)
    sha = need_commit(commit) if (commit or number is None) else None

    build: dict | None = None
    while True:
        if number is not None:
            try:
                build = client.get(f"repos/{slug}/builds/{number}")
            except NotFoundError:
                raise NotFoundError(f"no build #{number} in {slug}.") from None
        else:
            found = _find_for_commit(client, slug, sha, event=event, target=None)
            build = found[0] if found else None
            if build is None:
                # "Not yet" is NORMAL right after a push: the webhook has to fire
                # and the build has to be created. Only after a grace period is
                # this a real, distinct failure.
                if time.monotonic() >= appear_deadline:
                    raise CommitNotBuilt(
                        f"no {event} build appeared for commit {sha[:10]} in {slug} within "
                        f"{appear_timeout}. The push may not have reached Drone (webhook?), "
                        f"the repo may not be enabled, or the pipeline may not trigger on "
                        f"'{event}'. Check: drone-cli build ls --repo {slug}",
                        detail={"commit": sha, "repo": slug, "event": event},
                    )
                time.sleep(interval)
                continue
            number = build["number"]

        if B.is_done(build):
            break
        if time.monotonic() >= deadline:
            raise BuildNotFinished(
                f"build #{number} in {slug} was still {build.get('status')} after {timeout}.",
                detail={"repo": slug, "number": number, "status": build.get("status")},
            )
        time.sleep(interval)

    repo_obj = client.get(f"repos/{slug}") if links else None
    out = _decorate(obj, repo_obj, build, links)
    out["failed_steps"] = B.failed_steps(build)
    out["succeeded"] = B.succeeded(build)

    if B.is_blocked(build):
        stages = [s.get("number") for s in (build.get("stages") or []) if s.get("status") == "blocked"]
        stage_hint = stages[0] if stages else 1
        out["blocked"] = True
        out["note"] = (
            f"build #{number} is BLOCKED awaiting approval. "
            f"Approve it with: drone-cli build approve {number} --stage {stage_hint} --repo {slug}"
        )
    elif not B.succeeded(build):
        out["note"] = f"build #{number} {build.get('status')}. Logs: drone-cli log failed {number} --repo {slug}"

    obj.emitter.emit(out)

    if exit_code and not B.succeeded(build):
        raise typer.Exit(code=EXIT_BUILD_BLOCKED if B.is_blocked(build) else EXIT_BUILD_FAILED)


def promote(
    ctx: typer.Context,
    number: int = typer.Argument(None, help="Build NUMBER to promote. Prefer --commit."),
    repo: str = typer.Option(None, "--repo", "-r", help="owner/name."),
    commit: str = typer.Option(None, "--commit", "-c", help="Promote THIS commit's build (accepts HEAD)."),
    to: str = typer.Option(None, "--to", "-t", help="Target environment. Default: your `promote_target` setting (ships as prod)."),
    param: list[str] = typer.Option(None, "--param", "-P", help="KEY=VALUE parameter (repeatable)."),
    links: bool = typer.Option(False, "--links", help="Include derived commit/repo web URLs."),
) -> None:
    """Promote a build (or a commit) to a target environment.

        drone-cli promote --commit HEAD              # -> prod, by default
        drone-cli promote --commit HEAD --to staging

    Creates a NEW build with event=promote from the same commit; it does not
    re-run the original. Change the default target with
    `drone-cli settings set-promote-target`.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    slug = need_repo(obj, repo)
    target = (to or obj.config.promote_target).strip()

    if number is None:
        sha = need_commit(commit)
        # Promote the push build for that commit -- promoting a promote is
        # legal but almost never what someone means.
        found = _find_for_commit(client, slug, sha, event=None, target=None)
        pushes = [b for b in found if b.get("event") == B.EVENT_PUSH] or found
        if not pushes:
            raise CommitNotBuilt(
                f"no build found for commit {sha[:10]} in {slug} — nothing to promote."
            )
        src = pushes[0]
        number = src["number"]
        if not B.succeeded(src):
            # Refusing outright would be wrong (people do promote a red build on
            # purpose), but doing it silently would be worse.
            obj.emitter.message(
                f"warning: build #{number} for {sha[:10]} is {src.get('status')}, not success."
            )

    params: dict = {"target": target}
    for p in param or []:
        if "=" not in p:
            raise OpError(f"--param must be KEY=VALUE, got {p!r}")
        k, v = p.split("=", 1)
        params[k] = v

    build = client.post(f"repos/{slug}/builds/{number}/promote", params=params)
    repo_obj = client.get(f"repos/{slug}") if links else None
    out = _decorate(obj, repo_obj, build, links)
    out["promoted_from"] = number
    out["target"] = target
    out["note"] = (
        f"promotion queued as build #{build.get('number')} (event=promote, target={target}). "
        f"Wait for it: drone-cli wait {build.get('number')} --repo {slug}"
    )
    obj.emitter.emit(out)


def _duration(text: str) -> float:
    """Parse 90s / 30m / 2h / 45 (bare seconds) into seconds."""
    t = (text or "").strip().lower()
    if not t:
        raise OpError("a duration is required, e.g. 30m")
    units = {"s": 1, "m": 60, "h": 3600}
    if t[-1] in units:
        try:
            return float(t[:-1]) * units[t[-1]]
        except ValueError as exc:
            raise OpError(f"bad duration {text!r}, e.g. 90s, 30m, 2h") from exc
    try:
        return float(t)
    except ValueError as exc:
        raise OpError(f"bad duration {text!r}, e.g. 90s, 30m, 2h") from exc
