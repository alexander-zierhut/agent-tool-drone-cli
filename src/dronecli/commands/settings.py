"""`drone-cli settings` — every setting has a sane default or is asked once."""

from __future__ import annotations

import typer
from agentcli import OutputFormat
from agentcli.errors import OpError

from ..config import COMMIT_URL_PATTERNS, Config, config_path
from ..spec import SPEC, credentials
from ._shared import ctx_obj

app = typer.Typer(no_args_is_help=True)


@app.command()
def show(ctx: typer.Context) -> None:
    """Show every setting, its value, and where it came from."""
    obj = ctx_obj(ctx)
    cfg: Config = obj.config
    obj.emitter.emit(
        {
            "defaultFormat": cfg.default_format or "json (default — not yet chosen)",
            "promoteTarget": cfg.promote_target,
            "scmFlavour": cfg.scm_flavour,
            "scmBaseUrl": cfg.scm_base_url or "(derived from the repo's link)",
            "activeProfile": cfg.active_profile_name(),
            "credentialBackend": credentials.backend_name(),
            "configPath": str(config_path()),
        }
    )


@app.command("set-format")
def set_format(
    ctx: typer.Context,
    fmt: str = typer.Argument(..., help="json | table | markdown | csv"),
) -> None:
    """Set the default output format."""
    obj = ctx_obj(ctx)
    try:
        chosen = OutputFormat.coerce(fmt)
    except ValueError as exc:
        raise OpError(str(exc)) from exc
    obj.config.default_format = chosen.value
    obj.config.save()
    obj.emitter.emit({"status": "saved", "defaultFormat": chosen.value, "configPath": str(config_path())})


@app.command("set-promote-target")
def set_promote_target(
    ctx: typer.Context,
    target: str = typer.Argument(..., help="Default target for `promote` when --to is omitted, e.g. prod."),
) -> None:
    """Set the default promotion target (ships as `prod`)."""
    obj = ctx_obj(ctx)
    target = target.strip()
    if not target:
        raise OpError("a target is required, e.g. prod")
    obj.config.promote_target = target
    obj.config.save()
    obj.emitter.emit({"status": "saved", "promoteTarget": target, "configPath": str(config_path())})


@app.command("set-scm")
def set_scm(
    ctx: typer.Context,
    flavour: str = typer.Argument(..., help=f"One of: {', '.join(sorted(COMMIT_URL_PATTERNS))}"),
) -> None:
    """Set which URL shape to use for commit links.

    Drone's `repo.scm` field is an empty string even on a fully synced repo
    (verified live), so the provider cannot be detected — hence this setting.
    Gitea/Forgejo/GitHub share `/commit/{sha}`; GitLab and Bitbucket differ.
    """
    obj = ctx_obj(ctx)
    key = flavour.strip().lower()
    if key not in COMMIT_URL_PATTERNS:
        raise OpError(f"unknown scm '{flavour}'. Choose one of: {', '.join(sorted(COMMIT_URL_PATTERNS))}")
    obj.config.scm_flavour = key
    obj.config.save()
    obj.emitter.emit(
        {"status": "saved", "scmFlavour": key, "commitUrlPattern": COMMIT_URL_PATTERNS[key]}
    )


@app.command("set-scm-base-url")
def set_scm_base_url(
    ctx: typer.Context,
    url: str = typer.Argument(..., help="Repo web URL base, or 'none' to go back to deriving it."),
) -> None:
    """Override the base URL used to build commit links.

    Only needed when the repo's own `link` is wrong or unreachable from where you
    are (e.g. it points at an internal hostname).
    """
    obj = ctx_obj(ctx)
    val = None if url.strip().lower() in ("none", "-", "") else url.strip().rstrip("/")
    obj.config.scm_base_url = val
    obj.config.save()
    obj.emitter.emit({"status": "saved", "scmBaseUrl": val or "(derived from the repo's link)"})


@app.command()
def path(ctx: typer.Context) -> None:
    """Print the config file path."""
    ctx_obj(ctx).emitter.emit({"configPath": str(config_path()), "configDir": str(SPEC.config_dir())})
