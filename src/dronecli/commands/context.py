"""Session context — sticky option defaults applied to later commands.

A context is a small map of option-name -> value (e.g. ``repo=octocat/hello``).
Once set, those values become the *default* for matching options on later
commands, so you stop repeating ``--repo`` on every call — which matters more
here than in the sibling CLIs, since nearly every Drone endpoint is
``/api/repos/{owner}/{name}/…``. Explicit flags always win, ``--no-context``
ignores the context for one command, and contexts can be saved by name.

The mechanism is Click's ``default_map``, wired up in ``cli.py::_context_default_map``:
a key applies to a command **only** if that command has an *option* whose name
matches the key exactly. There is no error when it doesn't — the key simply does
nothing, forever. That is why ``KNOWN_KEYS`` below is not documentation: it is
the input to ``context set``'s own signature, and a test asserts every entry has
a real consumer somewhere in the tree.
"""

from __future__ import annotations

import os

import typer
from agentcli.errors import OpError

from ..config import Config, config_path
from ._shared import ctx_obj

app = typer.Typer(no_args_is_help=True)

#: Keys the context can hold. Each MUST match the *name* of an option on at least
#: one command (see tests/test_context_unit.py) — a key with no consumer is a
#: silent no-op, not a warning.
#:
#: Deliberately absent: `build` (a pinned number ages into staleness; the build is
#: the one value a caller always means freshly) and `status`/`event` (a sticky
#: `--status failure` would silently hide every passing build from `build ls`).
KNOWN_KEYS = ["repo", "owner", "branch"]

_KEY_HELP = {
    "repo": "Default repo, as an owner/name slug (not two keys, not an id).",
    # Its consumers spell the flag `--org` (orgsecret ls --org acme) but name the
    # parameter `owner`, and default_map matches the NAME. Say so, or `--owner`
    # setting a thing called `--org` looks like a bug.
    "owner": "Default namespace for org-scoped commands (`orgsecret --org`, templates).",
    "branch": "Default branch, e.g. main.",
}


def _validate(key: str, value: str) -> str:
    """Validate at `context set` time, not at Click's converter.

    A context value is injected as an option's *default*, so a bad one surfaces
    later, on an unrelated command, pointing at a flag the caller never passed.
    Reject it here, where the caller can still see what they typed.
    """
    v = (value or "").strip()
    if not v:
        raise OpError(f"context key '{key}' cannot be empty — use `context unset {key}` to remove it.")
    if key == "repo" and v.count("/") != 1:
        raise OpError(
            f"repo must be a single 'owner/name' slug, got {v!r}. "
            f"Drone addresses repos by slug — never by id."
        )
    if key == "owner" and "/" in v:
        raise OpError(f"owner is a bare namespace with no slash, got {v!r}. Did you mean --repo?")
    return v


@app.command("set")
def set_context(
    ctx: typer.Context,
    repo: str = typer.Option(None, "--repo", "-r", help=_KEY_HELP["repo"]),
    owner: str = typer.Option(None, "--owner", help=_KEY_HELP["owner"]),
    branch: str = typer.Option(None, "--branch", "-b", help=_KEY_HELP["branch"]),
    extra: list[str] = typer.Option(None, "--set", help="Generic key=value (repeatable). Must be a known key."),
) -> None:
    """Set/merge sticky defaults. Applies to later commands' matching options.

        drone-cli context set --repo octocat/hello-world

    Then `drone-cli build ls` behaves like `drone-cli build ls --repo octocat/hello-world`.
    """
    obj = ctx_obj(ctx)
    cfg = Config.load()

    updates = {k: v for k, v in (("repo", repo), ("owner", owner), ("branch", branch)) if v is not None}
    for item in extra or []:
        if "=" not in item:
            raise OpError(f"--set expects key=value, got {item!r}")
        k, v = item.split("=", 1)
        updates[k.strip()] = v.strip()

    if not updates:
        raise OpError(f"nothing to set — pass e.g. --repo owner/name. Known keys: {', '.join(KNOWN_KEYS)}")

    # An unknown key would save cleanly and then do nothing at all, because
    # `_context_default_map` only injects into options that exist. Refuse it
    # rather than hand back a context that lies about what it will scope.
    unknown = [k for k in updates if k not in KNOWN_KEYS]
    if unknown:
        raise OpError(
            f"unknown context key(s): {', '.join(sorted(unknown))}. "
            f"Known keys: {', '.join(KNOWN_KEYS)}. A key with no matching option "
            f"would be stored and silently ignored."
        )

    cfg.context.update({k: _validate(k, v) for k, v in updates.items()})
    cfg.save()
    obj.emitter.emit({"status": "context updated", "context": cfg.context})


#: The only rung a context value can come from today — it is read from the config
#: file and from nowhere else. Emitted per key anyway, because `show`'s job is
#: answering "why is this scoped wrong?", and an answer that omits *where the
#: value came from* is half of one. When a second source appears (an env
#: override, a repo-local file), it slots in here and the output shape does not
#: move under the agents already parsing it.
SOURCE_SAVED = "saved"


@app.command()
def show(ctx: typer.Context) -> None:
    """Show the active context — each value, and where it came from.

    Run this FIRST whenever output looks wrongly scoped: this is implicit state
    that changes results, and nothing echoes it back on a normal command.

    Every key is reported as `{"value": ..., "from": ...}` — `from` is `saved`
    for anything set with `context set`, i.e. everything, for now. Read `applies`
    before believing any of it: `--no-context` suspends the whole context for one
    command, and then these values are saved but NOT in force.
    """
    obj = ctx_obj(ctx)
    cfg = Config.load()
    obj.emitter.emit(
        {
            "context": {k: {"value": v, "from": SOURCE_SAVED} for k, v in cfg.context.items()},
            # Whether the context is live right now, not merely non-empty:
            # `--no-context` (popped into this env var by cli.py) suspends it.
            "applies": bool(cfg.context) and os.environ.get("DRONECLI_NO_CONTEXT") != "1",
            "saved": sorted(cfg.contexts),
            "knownKeys": KNOWN_KEYS,
            "configPath": str(config_path()),
        }
    )


@app.command()
def unset(
    ctx: typer.Context,
    key: str = typer.Argument(..., help=f"Context key to remove. One of: {', '.join(KNOWN_KEYS)}."),
) -> None:
    """Remove one key from the active context."""
    obj = ctx_obj(ctx)
    cfg = Config.load()
    cfg.context.pop(key, None)
    cfg.save()
    obj.emitter.emit({"status": "unset", "key": key, "context": cfg.context})


@app.command()
def clear(ctx: typer.Context) -> None:
    """Clear the active context entirely. Saved contexts are untouched."""
    obj = ctx_obj(ctx)
    cfg = Config.load()
    cfg.context = {}
    cfg.save()
    obj.emitter.emit({"status": "context cleared"})


@app.command()
def save(ctx: typer.Context, name: str = typer.Argument(..., help="Name to save the current context as.")) -> None:
    """Save the active context under a name for later reuse."""
    obj = ctx_obj(ctx)
    cfg = Config.load()
    if not cfg.context:
        raise OpError("active context is empty — set something first with `context set --repo owner/name`.")
    cfg.contexts[name] = dict(cfg.context)
    cfg.save()
    obj.emitter.emit({"status": "saved", "name": name, "context": cfg.context})


@app.command()
def use(ctx: typer.Context, name: str = typer.Argument(..., help="Saved context to activate.")) -> None:
    """Load a saved context as the active one. Replaces the active context wholesale."""
    obj = ctx_obj(ctx)
    cfg = Config.load()
    if name not in cfg.contexts:
        raise OpError(f"no saved context '{name}'. Saved: {', '.join(sorted(cfg.contexts)) or '(none)'}")
    cfg.context = dict(cfg.contexts[name])
    cfg.save()
    obj.emitter.emit({"status": "active", "name": name, "context": cfg.context})


@app.command("list")
def list_contexts(ctx: typer.Context) -> None:
    """List saved contexts."""
    obj = ctx_obj(ctx)
    cfg = Config.load()
    rows = [{"name": n, "context": c} for n, c in sorted(cfg.contexts.items())]
    obj.emitter.emit(
        rows,
        columns=[
            ("Name", "name"),
            ("Context", lambda r: ", ".join(f"{k}={v}" for k, v in r["context"].items())),
        ],
        empty="(no saved contexts)",
    )


@app.command()
def rm(ctx: typer.Context, name: str = typer.Argument(..., help="Saved context to delete.")) -> None:
    """Delete a saved context. Does not touch the active one, even if it came from here."""
    obj = ctx_obj(ctx)
    cfg = Config.load()
    cfg.contexts.pop(name, None)
    cfg.save()
    obj.emitter.emit({"status": "deleted", "name": name})
