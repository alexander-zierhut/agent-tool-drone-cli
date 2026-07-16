"""Drone-specific additions to the shared exit-code taxonomy.

The base contract lives in :mod:`agentcli.errors` and is identical across every
tool in the family — 0 ok · 1 generic · 3 config · 4 auth · 5 not-found ·
6 conflict · 7 validation · 130 SIGINT. Import from here so a command module has
one place to look.

**Codes 6 and 7 are RESERVED here, not reused.** Drone has no optimistic locking
(nothing 409s) and returns 400 rather than 422. It is tempting to recycle the
numbers — don't. The taxonomy is published API across the whole family, and an
agent that learned "6 means conflict" from OpenProject must never meet a Drone
CLI where 6 means something else. Leave the hole.

Drone adds two, and both were earned by observation rather than guessed:
"""

from __future__ import annotations

from agentcli.errors import (  # noqa: F401  (re-exported for command modules)
    ApiError,
    AuthError,
    ConfigError,
    ConflictError,
    DryRun,
    NotFoundError,
    OpError,
    ValidationError,
)


class NotImplementedOnServer(OpError):
    """HTTP 501 — the server compiled this feature out, or hosts it elsewhere.

    Exit **8**. The published ``drone/drone`` image does NOT 501 (verified live:
    repo secrets, org secrets, crons, templates, promote and admin users all
    returned 200 — the ``-tags oss`` build is a compile check that is never
    shipped). But Drone Cloud disables org secrets, and someone *could* build
    with ``-tags oss``, so an agent needs to be able to branch on "this server
    cannot do that" rather than parsing prose.
    """

    exit_code = 8


class BuildNotFinished(OpError):
    """A wait/watch hit its deadline before the build reached a terminal state.

    Exit **9**. Deliberately NOT "the build failed": the CLI did not observe a
    failure, it observed nothing. Conflating the two would make a slow queue
    look like a broken pipeline.
    """

    exit_code = 9


class CommitNotBuilt(OpError):
    """No build exists for the requested commit.

    Exit **10**, and the reason it needs its own code: after a push there is real
    latency before the webhook creates a build, so "no build yet" is *normal* and
    transient — while "no build, ever" (the commit was never pushed, the repo
    isn't enabled, the webhook is misconfigured) is a hard error. An agent
    waiting on its own commit must be able to tell those apart without a regex.
    """

    exit_code = 10
