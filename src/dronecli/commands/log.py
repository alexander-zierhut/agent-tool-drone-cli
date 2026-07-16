"""`drone-cli log` — build logs, and the failing step on its own.

This is the biggest token lever in the tool. Without `log failed`, finding out
why a build broke costs: GET the build → walk `stages[]` → know that stage/step
are 1-based *numbers* (not names, not ids) → GET that step's logs → strip the
`{pos,out,time}` envelope. Four calls and three pieces of tribal knowledge — and
the payload is unbounded prose that will spend an agent's context on `apt-get`
noise before it ever reaches the error.
"""

from __future__ import annotations

import typer
from agentcli.errors import NotFoundError

from .. import builds as B
from ..errors import CommitNotBuilt
from ._shared import ctx_obj, need_commit, need_repo

app = typer.Typer(no_args_is_help=True)


def _lines(client, slug: str, number: int, stage: int, step: int) -> list[dict]:
    """Fetch one step's log lines.

    A 404 here means the step never ran — which is information, not an error.
    """
    try:
        got = client.get(f"repos/{slug}/builds/{number}/logs/{stage}/{step}")
    except NotFoundError:
        return []
    return got if isinstance(got, list) else []


def _render(lines: list[dict], tail: int | None, grep: str | None) -> str:
    out = [(l.get("out") or "").rstrip("\n") for l in lines]
    if grep:
        out = [l for l in out if grep.lower() in l.lower()]
    if tail:
        out = out[-tail:]
    return "\n".join(out)


def _resolve_number(client, obj, slug: str, number: int | None, commit: str | None, event: str | None) -> int:
    if number is not None:
        return number
    sha = need_commit(commit)
    all_builds = list(client.paginate(f"repos/{slug}/builds", limit=200))
    found = B.select_for_commit(all_builds, sha, event=event)
    if not found:
        raise CommitNotBuilt(f"no build found for commit {sha[:10]} in {slug}.")
    return found[0]["number"]


@app.command("view")
def view(
    ctx: typer.Context,
    number: int = typer.Argument(None, help="Build NUMBER. Or use --commit."),
    repo: str = typer.Option(None, "--repo", "-r", help="owner/name."),
    commit: str = typer.Option(None, "--commit", "-c", help="Address by commit (accepts HEAD)."),
    stage: int = typer.Option(1, "--stage", help="Stage number (1-based ordinal, not an id)."),
    step: int = typer.Option(..., "--step", help="Step number (1-based ordinal, not a name)."),
    tail: int = typer.Option(None, "--tail", help="Only the last N lines."),
    grep: str = typer.Option(None, "--grep", help="Only lines containing this (case-insensitive)."),
    raw: bool = typer.Option(False, "--raw", help="Emit the {pos,out,time} envelope as JSON instead of text."),
) -> None:
    """Print one step's log.

    OUTPUT IS TEXT, NOT JSON — the one place this CLI breaks the JSON contract,
    because logs are prose. Use --raw for the structured envelope.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    slug = need_repo(obj, repo)
    number = _resolve_number(client, obj, slug, number, commit, None)

    lines = _lines(client, slug, number, stage, step)
    if raw:
        obj.emitter.emit(lines)
        return
    if not lines:
        obj.emitter.emit(
            {"repo": slug, "number": number, "stage": stage, "step": step,
             "lines": 0, "note": "no logs — that step never ran, or the logs were purged."}
        )
        return
    typer.echo(_render(lines, tail, grep))


@app.command("failed")
def failed(
    ctx: typer.Context,
    number: int = typer.Argument(None, help="Build NUMBER. Or use --commit."),
    repo: str = typer.Option(None, "--repo", "-r", help="owner/name."),
    commit: str = typer.Option(None, "--commit", "-c", help="Address by commit (accepts HEAD)."),
    event: str = typer.Option(None, "--event", help="With --commit: which build to pick."),
    tail: int = typer.Option(60, "--tail", help="Lines of context from the end of each failing step."),
    grep: str = typer.Option(None, "--grep", help="Only lines containing this."),
    all_steps: bool = typer.Option(False, "--all", help="Every failing step, not just the first."),
) -> None:
    """Print the log of the failing step — and nothing else.

        drone-cli log failed --commit HEAD
        drone-cli log failed 42 --tail 100

    The one command to reach for when CI is red. Resolves the build, finds which
    step actually failed, and prints only its tail.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    slug = need_repo(obj, repo)
    number = _resolve_number(client, obj, slug, number, commit, event)

    build = client.get(f"repos/{slug}/builds/{number}")
    fails = B.failed_steps(build)

    if not fails:
        obj.emitter.emit(
            {
                "repo": slug,
                "number": number,
                "status": build.get("status"),
                "failed_steps": [],
                "note": (
                    f"build #{number} is {build.get('status')} — no failing step to show."
                    if B.succeeded(build)
                    else f"build #{number} is {build.get('status')} but no step reported failure "
                         f"(it may have been cancelled, or never started)."
                ),
            }
        )
        return

    picked = fails if all_steps else fails[:1]
    for f in picked:
        lines = _lines(client, slug, number, f["stage"], f["step"])
        header = (
            f"=== build #{number} · stage {f['stage']} ({f['stage_name']}) "
            f"· step {f['step']} ({f['step_name']}) · {f['status']} exit={f['exit_code']} ==="
        )
        typer.echo(header)
        typer.echo(_render(lines, tail, grep) or "(no log output)")
        typer.echo("")

    if not all_steps and len(fails) > 1:
        typer.echo(f"({len(fails) - 1} more failing step(s) — rerun with --all)")
