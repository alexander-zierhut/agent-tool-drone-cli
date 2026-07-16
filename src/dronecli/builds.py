"""Build selection, terminal-state logic, and commit→build resolution.

This module exists because of one requirement: **address builds by commit, not by
build number**. An agent that has just pushed knows its SHA; it does not know a
build number, and "the latest build" is a race — two people pushing at once means
the newest build may be someone else's, and waiting on it reports their failure
as yours.

Pure functions over already-fetched data wherever possible, so the interesting
logic is testable without a server.
"""

from __future__ import annotations

import subprocess
from typing import Any, Iterable

# Drone's build statuses. Read from core/build.go; the spike observed
# pending/running/success/failure/killed live.
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_BLOCKED = "blocked"
STATUS_WAITING = "waiting_on_dependencies"
STATUS_DECLINED = "declined"
STATUS_SUCCESS = "success"
STATUS_FAILURE = "failure"
STATUS_KILLED = "killed"
STATUS_ERROR = "error"
STATUS_SKIPPED = "skipped"

#: Not finished, and will move on its own without anyone intervening.
IN_FLIGHT = frozenset({STATUS_PENDING, STATUS_RUNNING, STATUS_WAITING})

#: Finished, and will never change again.
TERMINAL = frozenset(
    {STATUS_SUCCESS, STATUS_FAILURE, STATUS_KILLED, STATUS_ERROR, STATUS_SKIPPED, STATUS_DECLINED}
)

#: Statuses an agent should treat as "the pipeline is fine".
GOOD = frozenset({STATUS_SUCCESS, STATUS_SKIPPED})

EVENT_PUSH = "push"
EVENT_PROMOTE = "promote"
EVENT_ROLLBACK = "rollback"
EVENT_CUSTOM = "custom"
EVENT_CRON = "cron"
EVENT_TAG = "tag"
EVENT_PULL_REQUEST = "pull_request"


def is_done(build: dict) -> bool:
    """Has this build stopped moving on its own?

    Drone's own ``Build.IsDone()`` says not-done while
    ``waiting_on_dependencies|pending|running|blocked``. **We deliberately
    deviate on `blocked`.**

    A blocked build is waiting for a *human* to approve a stage. It will sit
    there for hours. Copying ``IsDone()`` exactly means `wait` spins until its
    timeout on every approval-gated pipeline — precisely the hang this feature
    exists to prevent. So `blocked` is terminal *for waiting purposes* and is
    reported as its own distinct outcome, with the command needed to unblock it.

    If you "fix" this back to match upstream, write a test first.
    """
    return build.get("status") not in IN_FLIGHT


def is_blocked(build: dict) -> bool:
    return build.get("status") == STATUS_BLOCKED


def succeeded(build: dict) -> bool:
    return build.get("status") in GOOD


def normalize_sha(sha: str) -> str:
    return (sha or "").strip().lower()


def sha_matches(build: dict, sha: str) -> bool:
    """Does *build* belong to commit *sha*?

    Accepts a short SHA (prefix match), because that is what humans paste and
    what ``git rev-parse --short HEAD`` prints. ``after`` is the commit the build
    ran against; ``before`` is its parent and must not match, or a push would
    also match its own predecessor's build.
    """
    want = normalize_sha(sha)
    if not want:
        return False
    after = normalize_sha(build.get("after") or "")
    return bool(after) and after.startswith(want)


def select_for_commit(
    builds: Iterable[dict],
    sha: str,
    *,
    event: str | None = None,
    target: str | None = None,
) -> list[dict]:
    """Every build for *sha*, newest first, optionally filtered.

    One commit legitimately has MANY builds: the original push, a restart (which
    mints a new number), and any promotes. Callers must say which they mean
    rather than trusting "the latest".
    """
    out = [b for b in builds if sha_matches(b, sha)]
    if event:
        out = [b for b in out if b.get("event") == event]
    if target:
        out = [b for b in out if (b.get("deploy_to") or "") == target]
    out.sort(key=lambda b: b.get("number", 0), reverse=True)
    return out


def latest_promotion(builds: Iterable[dict], target: str) -> dict | None:
    """The newest SUCCESSFUL promote to *target* — i.e. what is actually deployed.

    Answers "which commit is currently on prod?". Successful only, on purpose: a
    failed promote did not deploy anything, and reporting its commit as live is
    the single most dangerous wrong answer this tool could give.
    """
    cands = [
        b
        for b in builds
        if b.get("event") in (EVENT_PROMOTE, EVENT_ROLLBACK)
        and (b.get("deploy_to") or "") == target
        and succeeded(b)
    ]
    if not cands:
        return None
    return max(cands, key=lambda b: (b.get("number", 0)))


def promotions_of(builds: Iterable[dict], sha: str, target: str | None = None) -> list[dict]:
    """Every promote/rollback of *sha*, newest first. Answers "has this been promoted?"."""
    out = [
        b
        for b in builds
        if sha_matches(b, sha) and b.get("event") in (EVENT_PROMOTE, EVENT_ROLLBACK)
    ]
    if target:
        out = [b for b in out if (b.get("deploy_to") or "") == target]
    out.sort(key=lambda b: b.get("number", 0), reverse=True)
    return out


def failed_steps(build: dict) -> list[dict]:
    """Every failed step, as ``{stage, step, stage_name, step_name, exit_code}``.

    Stage and step are **1-based ordinals**, not database ids and not names — the
    log endpoint addresses them positionally, and passing an id silently fetches
    the wrong step's logs (or 404s). This is the tribal knowledge that makes
    ``log failed`` worth shipping.

    Note ``stages`` is ``null`` (not ``[]``) on a pending build, which NPEs any
    naive iteration.
    """
    out: list[dict] = []
    for stage in build.get("stages") or []:
        for step in stage.get("steps") or []:
            if step.get("status") in (STATUS_FAILURE, STATUS_ERROR, STATUS_KILLED):
                out.append(
                    {
                        "stage": stage.get("number"),
                        "step": step.get("number"),
                        "stage_name": stage.get("name"),
                        "step_name": step.get("name"),
                        "status": step.get("status"),
                        "exit_code": step.get("exit_code"),
                    }
                )
    return out


def duration_seconds(obj: dict) -> int | None:
    """Wall time of a build/stage/step.

    Drone exposes **no duration field anywhere** — only raw epochs. Everything
    time-related in this CLI is derived here.

    ``finished == 0`` means "still running", NOT 1970: treating the epoch as a
    timestamp yields a 56-year duration. Return None and let the caller exclude it.
    """
    started, finished = obj.get("started") or 0, obj.get("finished") or 0
    if not started or not finished or finished < started:
        return None
    return int(finished - started)


def queue_seconds(build: dict) -> int | None:
    """How long the build waited before a runner picked it up."""
    created, started = build.get("created") or 0, build.get("started") or 0
    if not created or not started or started < created:
        return None
    return int(started - created)


def git_head(cwd: str | None = None) -> str | None:
    """The local checkout's HEAD SHA, so `--commit HEAD` works.

    Best-effort: returns None outside a git repo rather than raising. The whole
    point is `drone-cli wait --commit HEAD` right after a push.
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return (out.stdout or "").strip() or None


def resolve_commit(value: str | None, cwd: str | None = None) -> str | None:
    """Turn ``HEAD`` (or a literal SHA) into a SHA."""
    if not value:
        return None
    if value.strip().upper() == "HEAD":
        return git_head(cwd)
    return value.strip()
