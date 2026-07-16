"""`drone-cli deploy` — what is live, and what got it there.

Drone can promote, but it will not tell you *what is currently on prod*. That
answer exists only as an emergent property of the build list: the newest
successful promote/rollback to a target. Deriving it is the whole point.
"""

from __future__ import annotations

import typer

from .. import builds as B
from ._shared import ctx_obj, need_commit, need_repo

app = typer.Typer(no_args_is_help=True)

_COLUMNS = ["number", "status", "event", "target", "after", "author_login", "finished"]


def _scan(client, slug: str, pages: int = 200) -> list[dict]:
    return list(client.paginate(f"repos/{slug}/builds", limit=pages))


def _shape(obj, repo_obj, build: dict) -> dict:
    out = {
        "number": build.get("number"),
        "status": build.get("status"),
        "event": build.get("event"),
        "target": build.get("deploy_to"),
        "commit": build.get("after"),
        "message": (build.get("message") or "").strip(),
        "author": build.get("author_login"),
        "finished": build.get("finished"),
        "duration": B.duration_seconds(build),
    }
    url = obj.config.commit_url(repo_obj.get("link") or "", build.get("after") or "")
    if url:
        out["commit_url"] = url
    return out


@app.command("status")
def status(
    ctx: typer.Context,
    repo: str = typer.Option(None, "--repo", "-r", help="owner/name."),
    to: str = typer.Option(None, "--to", "-t", help="Target. Default: your promote_target setting (prod)."),
) -> None:
    """Which commit is currently deployed to a target?

        drone-cli deploy status              # what is on prod
        drone-cli deploy status --to staging

    Defined as the newest **successful** promote or rollback to that target. A
    failed promote deployed nothing, and reporting its commit as live would be
    the most dangerous wrong answer this tool could give.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    slug = need_repo(obj, repo)
    target = (to or obj.config.promote_target).strip()

    all_builds = _scan(client, slug)
    live = B.latest_promotion(all_builds, target)
    repo_obj = client.get(f"repos/{slug}")

    if live is None:
        obj.emitter.emit(
            {
                "repo": slug,
                "target": target,
                "deployed": None,
                "note": (
                    f"nothing has been successfully promoted to '{target}' in the last "
                    f"{len(all_builds)} builds. Promote with: drone-cli promote --commit HEAD --to {target}"
                ),
            }
        )
        return

    out = _shape(obj, repo_obj, live)
    out["repo"] = slug
    out["deployed"] = True
    obj.emitter.emit(out)


@app.command("of")
def of(
    ctx: typer.Context,
    repo: str = typer.Option(None, "--repo", "-r", help="owner/name."),
    commit: str = typer.Option("HEAD", "--commit", "-c", help="Commit to ask about (default: HEAD)."),
    to: str = typer.Option(None, "--to", "-t", help="Only this target."),
) -> None:
    """Has this commit been promoted — and where to?

        drone-cli deploy of --commit HEAD
        drone-cli deploy of --commit HEAD --to prod

    Also answers the follow-up an agent always needs: *is it still live, or has
    something newer replaced it?*
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    slug = need_repo(obj, repo)
    sha = need_commit(commit)

    all_builds = _scan(client, slug)
    proms = B.promotions_of(all_builds, sha, target=to)
    repo_obj = client.get(f"repos/{slug}")

    targets: dict[str, dict] = {}
    for p in proms:
        t = p.get("deploy_to") or ""
        if t and t not in targets and B.succeeded(p):
            live = B.latest_promotion(all_builds, t)
            targets[t] = {
                "target": t,
                "build": p.get("number"),
                "still_live": bool(live and live.get("number") == p.get("number")),
            }

    out = {
        "repo": slug,
        "commit": sha,
        "promoted": bool([p for p in proms if B.succeeded(p)]),
        "liveOn": sorted([t for t, v in targets.items() if v["still_live"]]),
        "promotions": [_shape(obj, repo_obj, p) for p in proms],
    }
    url = obj.config.commit_url(repo_obj.get("link") or "", sha)
    if url:
        out["commit_url"] = url
    if not proms:
        out["note"] = (
            f"commit {sha[:10]} has never been promoted. "
            f"Promote it: drone-cli promote --commit {sha[:10]} --to {obj.config.promote_target}"
        )
    obj.emitter.emit(out)


@app.command("ls")
def ls(
    ctx: typer.Context,
    repo: str = typer.Option(None, "--repo", "-r", help="owner/name."),
    to: str = typer.Option(None, "--to", "-t", help="Only this target."),
    limit: int = typer.Option(20, "--limit", "-n", help="Max rows."),
) -> None:
    """Deployment history — every promote/rollback, newest first."""
    obj = ctx_obj(ctx)
    client = obj.client()
    slug = need_repo(obj, repo)

    all_builds = _scan(client, slug)
    rows = [b for b in all_builds if b.get("event") in (B.EVENT_PROMOTE, B.EVENT_ROLLBACK)]
    if to:
        rows = [b for b in rows if (b.get("deploy_to") or "") == to]
    rows = sorted(rows, key=lambda b: b.get("number", 0), reverse=True)[:limit]
    repo_obj = client.get(f"repos/{slug}")
    obj.emitter.emit(
        [_shape(obj, repo_obj, b) for b in rows],
        columns=["number", "status", "target", "commit", "author", "duration"],
    )
