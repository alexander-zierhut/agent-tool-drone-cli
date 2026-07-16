"""`drone-cli auth` — log in, log out, inspect credentials."""

from __future__ import annotations

import sys

import typer
from agentcli.errors import AuthError, ConfigError, OpError

from ..client import Client
from ..config import Config, Profile
from ..spec import SPEC, credentials, token_url
from ._shared import ctx_obj

app = typer.Typer(no_args_is_help=True)


def _prompt(text: str, *, secret: bool = False) -> str:
    """Prompt on stderr so stdout stays a clean machine channel."""
    if secret:
        import getpass

        return getpass.getpass(text, stream=sys.stderr).strip()
    sys.stderr.write(text)
    sys.stderr.flush()
    return (sys.stdin.readline() or "").strip()


def _normalize_server(url: str) -> str:
    url = url.strip().rstrip("/")
    if not url:
        raise ConfigError("a server URL is required, e.g. https://drone.example.com")
    if not url.startswith(("http://", "https://")):
        # Nobody types the scheme. Assume TLS rather than rejecting.
        url = "https://" + url
    return url


@app.command()
def login(
    ctx: typer.Context,
    server: str = typer.Option(None, "--server", "-s", help="Drone server URL, e.g. https://drone.example.com."),
    token: str = typer.Option(None, "--token", "-t", help="API token. Get one at <server>/account."),
    name: str = typer.Option("default", "--name", help="Profile name (for multiple servers)."),
    insecure: bool = typer.Option(False, "--insecure", help="Skip TLS verification (self-signed certs)."),
) -> None:
    """Log in and store the token in your OS keyring.

    Interactive when flags are omitted: asks for the server, then — knowing the
    URL — tells you exactly where to get a token, rather than making you hunt.
    """
    obj = ctx_obj(ctx)

    if not server:
        if not sys.stdin.isatty():
            raise ConfigError("--server is required when stdin is not a terminal.")
        sys.stderr.write("\nDrone server URL (e.g. https://drone.example.com)\n")
        server = _prompt("Server: ")
    server = _normalize_server(server)

    if not token:
        if not sys.stdin.isatty():
            raise ConfigError("--token is required when stdin is not a terminal.")
        # The whole point: we know the server now, so show the exact page.
        sys.stderr.write(f"\nGet your API token here:\n  {token_url(server)}\n\n")
        token = _prompt("Token: ", secret=True)
    if not token:
        raise ConfigError("a token is required.")

    # Verify BEFORE persisting: storing a bad token just moves the failure to a
    # later, more confusing command.
    client = Client(server, token, verify_ssl=not insecure)
    try:
        user = client.get("user")
    except AuthError as exc:
        raise AuthError(
            f"that token was rejected by {server}. Get a fresh one at {token_url(server)}.",
            detail=getattr(exc, "detail", None),
        ) from exc

    cfg: Config = obj.config
    cfg.upsert_profile(
        Profile(name=name, base_url=server, username=user.get("login"), verify_ssl=not insecure),
        make_current=True,
    )
    backend = credentials.store_token(name, token)
    cfg.save()

    obj.emitter.emit(
        {
            "status": "logged in",
            "server": server,
            "profile": name,
            "login": user.get("login"),
            "admin": bool(user.get("admin")),
            "credentialBackend": backend,
        }
    )


@app.command()
def status(ctx: typer.Context) -> None:
    """Show the active profile and — importantly — WHICH token is actually in use.

    The precedence is env > keyring > file, so an exported DRONE_TOKEN silently
    overrides a keyring login. That is deliberate (CI depends on it) but
    confusing exactly when you can least afford it, so this command always names
    the backend that will actually speak.
    """
    obj = ctx_obj(ctx)
    cfg: Config = obj.config
    name = cfg.active_profile_name()
    try:
        prof = cfg.resolve()
        server = prof.base_url
    except ConfigError:
        server = None

    tok = credentials.get_token(name)
    out = {
        "profile": name,
        "server": server,
        "authenticated": bool(tok),
        "credentialBackend": credentials.backend_name(),
        "tokenEnvVars": list(SPEC.token_env_names()),
        "configPath": str(SPEC.config_file()),
    }
    if server:
        out["tokenUrl"] = token_url(server)

    # Say it out loud when the environment is beating the keyring.
    hit = credentials._env_token_hit()
    if hit and (SPEC.config_file().exists() or cfg.profiles):
        out["note"] = (
            f"${hit[0]} is set and takes precedence over any keyring login. "
            f"Unset it to use your stored credentials."
        )
    obj.emitter.emit(out)


@app.command()
def whoami(ctx: typer.Context) -> None:
    """Show the authenticated Drone user."""
    obj = ctx_obj(ctx)
    user = obj.client().get("user")
    obj.emitter.emit(
        {
            "id": user.get("id"),
            "login": user.get("login"),
            "email": user.get("email") or None,
            "admin": bool(user.get("admin")),
            "machine": bool(user.get("machine")),
            "active": bool(user.get("active")),
        },
        columns=["id", "login", "email", "admin"],
    )


@app.command()
def logout(
    ctx: typer.Context,
    name: str = typer.Option(None, "--name", help="Profile to log out (default: the active one)."),
) -> None:
    """Remove the stored token for a profile."""
    obj = ctx_obj(ctx)
    profile = name or obj.config.active_profile_name()
    credentials.delete_token(profile)
    obj.emitter.emit({"status": "logged out", "profile": profile})
