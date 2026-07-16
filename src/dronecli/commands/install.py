"""`drone-cli install claude` — register this CLI with Claude Code.

The idiomatic way to make a CLI discoverable to Claude Code is a **Skill**: a
``SKILL.md`` whose ``description`` tells Claude when to use the tool. This command
drops that skill into ``~/.claude/skills/drone-ci/`` (or the project's
``.claude/skills/``), and can optionally add a one-line hint to the user's
``~/.claude/CLAUDE.md`` memory. Everything is reversible with ``--uninstall``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import typer
from agentcli.errors import OpError

from .. import __version__
from ._shared import ctx_obj

app = typer.Typer(no_args_is_help=True)

SKILL_NAME = "drone-ci"
_MEM_START = "<!-- drone-cli:start -->"
_MEM_END = "<!-- drone-cli:end -->"

# The `description` is the ENTIRE matching surface — Claude sees this and nothing
# else when deciding whether to load the skill. So every trigger is anchored to
# the product noun ("Drone CI", "a Drone build", "Drone secrets"). Bare "build",
# "CI", "pipeline" or "deploy" would fire on every unrelated question about a
# failing webpack build, and a skill that over-fires gets distrusted and ignored.
SKILL_MD = f"""\
---
name: drone-ci
description: >-
  Work with Drone CI via the `drone-cli` command — check whether a commit's Drone
  build passed, wait for a Drone build after a push, read Drone build logs (including
  just the failing step), restart or cancel a Drone build, promote a Drone build to
  an environment, and manage Drone repos, Drone secrets and Drone crons. Use this
  whenever the user mentions Drone or Drone CI, asks if their push/commit passed CI
  in Drone, asks about a Drone build, a `.drone.yml` pipeline, a Drone deployment,
  or wants to query or change their Drone server.
---

# Drone CI CLI (agent-tool-drone-cli v{__version__})

The `drone-cli` command is installed on this machine and talks to the user's
Drone CI server over its REST API.

## Start here: address builds by COMMIT, not by build number

This is the tool's premise. Build numbers are racy — if a colleague pushes while
you do, "the latest build" may be theirs, and you would report their failure as
the user's. The commit SHA is the only stable handle you already know.

```
drone-cli wait --commit HEAD                     # did the commit we just pushed pass?
drone-cli wait --commit HEAD --exit-code && ./deploy.sh
drone-cli log failed --commit HEAD               # ONLY the failing step's log
drone-cli promote --commit HEAD --to staging
```

`wait` has three outcomes you must NOT conflate:
- **finished** — the result is in the JSON (`status`, `succeeded`, `failed_steps`).
- **blocked** — a human must approve; the exact approve command is in `note`.
- **never appeared** (exit 10) — the webhook may be broken or the repo not enabled
  in Drone. That is *not* "the tests failed"; do not report it as one.

## Learn the tool from the tool
- `drone-cli guide` — full operating manual. `drone-cli guide <topic>` for
  builds/logs/repos/secrets/context/gotchas/…
- `drone-cli <group> --help` for any command.

## Output contract
- Default output is JSON on stdout — parse it. **Exception: logs are raw text**
  (`log view`, `log failed`); do not `json.loads` a log dump.
- Errors are JSON on **stderr** with a non-zero exit code (`{{"error": ..., "status": 404}}`).
- Trim with `--fields number,status,after`; `-o table` for humans; `-o csv` to export.

## Exit codes
`0` ok · `1` generic · `2` usage · `3` config · `4` auth (401/403) · `5` not found ·
`7` validation (400) · `8` not implemented on this server (501) · `9` timed out waiting
for a build · `10` no build exists for that commit · `130` interrupted.

**A red build is exit 0** — the CLI ran fine, the outcome is in the JSON. Only
`wait --exit-code` opts into the 20-29 band (`20` build failed, `24` blocked awaiting
approval), deliberately far from the error codes.

## Auth
Uses `DRONE_SERVER` + `DRONE_TOKEN` env vars, or a stored profile
(`drone-cli auth status`). If not configured, ask the user to run
`drone-cli auth login --server <URL> --token <TOKEN>` (token: `<server>/account`).

## Make changes safely
Preview ANY write with `--dry-run` (prints the exact request, sends nothing).
Confirm destructive actions (cancel, disable, delete) with the user first.

## Stop repeating --repo
`drone-cli context set --repo owner/name` makes it the default for later commands.
Repos are always `owner/name` slugs, never ids. If output looks wrongly scoped,
run `drone-cli context show` or pass `--no-context`.

## Gotchas that bite once
- `drone-cli build run` triggers event **`custom`**, not `push`. A pipeline gated on
  `trigger: event: [push]` will skip it — that is the pipeline's config, not a bug.
- `build restart` creates a **new build number**, and the *previous* build's params win.
- Drone **secret values are never readable** — the API returns names only, by design.
  An agent that reads a secret back to verify gets nothing; that is not a failure.
- Stage and step are **1-based ordinals**, not names or ids. Build **number** != build **id**.

Anything not wrapped: `drone-cli raw get|post|patch|delete <path>` (paths are relative
to `<server>/api`).
"""

_MEMORY_HINT = (
    f"{_MEM_START}\n"
    "The `drone-cli` CLI (package agent-tool-drone-cli) is installed. It is an "
    "agent-ready Drone CI client with JSON output — `drone-cli wait --commit HEAD` "
    "answers 'did my push pass?'. Run `drone-cli guide` to learn it.\n"
    f"{_MEM_END}\n"
)


def claude_available() -> bool:
    """Best-effort: is Claude Code installed on this machine?

    Note this detects *Claude*, not Drone — it is unchanged from the sibling CLIs
    on purpose.
    """
    if shutil.which("claude"):
        return True
    home = Path.home()
    return (home / ".claude").is_dir() or (home / ".local" / "bin" / "claude").exists()


def _skill_dir(project: bool) -> Path:
    base = Path.cwd() if project else Path.home()
    return base / ".claude" / "skills" / SKILL_NAME


def skill_installed(project: bool = False) -> bool:
    return (_skill_dir(project) / "SKILL.md").exists()


def write_skill(project: bool = False) -> Path:
    d = _skill_dir(project)
    d.mkdir(parents=True, exist_ok=True)
    path = d / "SKILL.md"
    path.write_text(SKILL_MD)
    return path


def _memory_file() -> Path:
    return Path.home() / ".claude" / "CLAUDE.md"


def write_memory_hint() -> Path:
    path = _memory_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text() if path.exists() else ""
    if _MEM_START in existing:
        return path  # already present
    sep = "" if existing.endswith("\n") or not existing else "\n"
    path.write_text(existing + sep + "\n" + _MEMORY_HINT)
    return path


def _remove_memory_hint() -> bool:
    """Remove only our marked block — the file is the user's, and everything
    outside the markers is theirs, not ours to rewrite."""
    path = _memory_file()
    if not path.exists():
        return False
    text = path.read_text()
    if _MEM_START not in text or _MEM_END not in text:
        return False
    before, _, rest = text.partition(_MEM_START)
    _, _, after = rest.partition(_MEM_END)
    path.write_text((before.rstrip("\n") + "\n" + after.lstrip("\n")).strip("\n") + "\n")
    return True


@app.command()
def claude(
    ctx: typer.Context,
    project: bool = typer.Option(False, "--project", help="Install into ./.claude (this repo) instead of ~/.claude."),
    memory: bool = typer.Option(False, "--memory", help="Also add a one-line hint to ~/.claude/CLAUDE.md."),
    force: bool = typer.Option(False, "--force", help="Install even if Claude Code isn't detected."),
    uninstall: bool = typer.Option(False, "--uninstall", help="Remove the skill (and memory hint)."),
    print_: bool = typer.Option(False, "--print", help="Print the SKILL.md that would be written and exit."),
) -> None:
    """Register this CLI with Claude Code as a Skill so Claude auto-uses it.

    Writes ~/.claude/skills/drone-ci/SKILL.md (idiomatic discovery). Claude then
    invokes it whenever you mention Drone CI. Reversible with --uninstall.
    """
    obj = ctx_obj(ctx)

    if print_:
        typer.echo(SKILL_MD)
        return

    if uninstall:
        d = _skill_dir(project)
        removed = []
        if (d / "SKILL.md").exists():
            (d / "SKILL.md").unlink()
            try:
                d.rmdir()
            except OSError:
                pass  # the user put other files there; leaving them is correct
            removed.append(str(d))
        if _remove_memory_hint():
            removed.append(str(_memory_file()) + " (hint)")
        obj.emitter.emit({"status": "uninstalled", "removed": removed})
        return

    if not force and not claude_available():
        raise OpError(
            "Claude Code was not detected on this machine. Install it from "
            "https://claude.com/claude-code, or re-run with --force to install the skill anyway."
        )

    skill_path = write_skill(project)
    result = {
        "status": "installed",
        "skill": str(skill_path),
        "scope": "project" if project else "user",
        "note": (
            "Claude Code will use the `drone-cli` CLI automatically when you mention "
            "Drone CI. Start a new session to pick it up."
        ),
    }
    if memory:
        result["memoryHint"] = str(write_memory_hint())
    obj.emitter.emit(result)
