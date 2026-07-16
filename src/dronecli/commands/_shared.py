"""Helpers shared by command modules."""

from __future__ import annotations

import typer
from agentcli.errors import ConfigError

from ..appctx import AppContext
from ..builds import resolve_commit


def ctx_obj(ctx: typer.Context) -> AppContext:
    """The AppContext built by the root callback.

    Falls back to constructing one so a command can be unit-tested (or invoked
    via CliRunner) without the root callback having run.
    """
    obj = getattr(ctx, "obj", None)
    if obj is None:
        obj = AppContext()
        ctx.obj = obj
    return obj


def need_repo(obj: AppContext, repo: str | None) -> str:
    """Resolve the target repo slug, or explain how to set one.

    Order: explicit --repo > sticky context (injected as a Click default) > error.
    """
    slug = repo or (obj.config.context or {}).get("repo")
    if not slug:
        raise ConfigError(
            "no repo. Pass --repo owner/name, or set a sticky default with "
            "`drone-cli context set --repo owner/name`."
        )
    if "/" not in slug:
        raise ConfigError(f"repo must be 'owner/name', got {slug!r}.")
    return slug.strip("/")


def need_commit(commit: str | None) -> str:
    """Resolve --commit (accepts HEAD) or explain."""
    sha = resolve_commit(commit)
    if not sha:
        if commit and commit.strip().upper() == "HEAD":
            raise ConfigError(
                "could not read HEAD — this directory is not a git checkout. "
                "Pass an explicit --commit <sha>."
            )
        raise ConfigError("a --commit <sha> (or HEAD) is required.")
    return sha
