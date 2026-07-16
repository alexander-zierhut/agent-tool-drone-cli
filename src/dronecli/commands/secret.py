"""`drone-cli secret` — repo-scoped secrets.

The one fact that shapes every line here: **a secret's value is write-only.**
Drone's read *and* write handlers return `secret.Copy()`, which omits `Data`
(verified live: `GET .../secrets/{name}` -> `{id, repo_id, name}` and nothing
else). There is no flag, no scope and no admin token that reveals a value. So
this module's job is not "manage values" — it is to set them safely, to make the
names/flags auditable, and to say "impossible" fast and with a reason.

The redaction chokepoint lives HERE, not in the client. `--dry-run` is
intercepted inside `client.py` at the transport layer, which sees an opaque body
and cannot know a field is secret. If the value were handed to the client, a
dry run would print it. So a redacted body is built *before* the call — see
:func:`_body`.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import typer
from agentcli.errors import OpError

from ..errors import ConflictError, NotFoundError, ValidationError
from ._shared import ctx_obj, need_repo

app = typer.Typer(no_args_is_help=True)

COLUMNS = ["name", "pull_request", "pull_request_push"]

#: What a dry run prints instead of the value. A placeholder, never the secret.
REDACTED = "***REDACTED***"

#: Mirrors `core.Secret.Validate` — enforced client-side so a typo costs no
#: round trip and the error names the rule instead of echoing a bare 400.
NAME_RE = re.compile(r"^[a-zA-Z0-9_.-]+$")

WRITE_ONLY = (
    "secret values are write-only: Drone's handlers return secret.Copy(), which omits the "
    "value, so no command or flag can read one back. `data: null` here means 'unreadable', "
    "NOT 'empty'. To rotate, overwrite it: `drone-cli secret set NAME --from-env VAR`."
)

PR_WARNING = (
    "pull_request=true exposes this secret to pull_request builds. Anyone who can open a PR "
    "against this repo (including from a fork, unless the repo sets ignore_forks) can add a "
    "pipeline step that prints it. Only do this for secrets a PR author may already read."
)


# ---------------------------------------------------------------------------
# value plumbing
# ---------------------------------------------------------------------------


def validate_name(name: str) -> str:
    """Reject a bad name here rather than let the server return a bare 400."""
    n = (name or "").strip()
    if not n:
        raise ValidationError("a secret name is required.")
    if not NAME_RE.match(n):
        raise ValidationError(
            f"invalid secret name {name!r}: only letters, digits and . _ - are allowed "
            f"(Drone's own rule; a name with a slash or space is rejected server-side)."
        )
    return n


def _strip_one_newline(text: str) -> str:
    """Drop exactly one trailing newline.

    `echo hunter2 | drone-cli secret set TOKEN --from-stdin` and every text
    editor append one. A token stored with a trailing "\\n" fails auth at build
    time with a message that points at the *service*, never at the secret — the
    single most expensive way to learn this. Only ONE is stripped, so a value
    that genuinely ends in a blank line survives as long as it is written with two.
    """
    if text.endswith("\r\n"):
        return text[:-2]
    if text.endswith("\n"):
        return text[:-1]
    return text


def read_value(
    value: str | None,
    from_env: str | None,
    from_file: str | None,
    from_stdin: bool,
) -> str:
    """Resolve exactly one value source, or explain the menu."""
    chosen = [
        n
        for n, on in (
            ("--value", value is not None),
            ("--from-env", bool(from_env)),
            ("--from-file", bool(from_file)),
            ("--from-stdin", bool(from_stdin)),
        )
        if on
    ]
    if not chosen:
        raise OpError(
            "no value. Pass exactly one of --from-env VAR (safest), --from-file PATH, "
            "--from-stdin, or --value TEXT."
        )
    if len(chosen) > 1:
        raise OpError(f"pass exactly one value source, got {', '.join(chosen)}.")

    if value is not None:
        data = value
    elif from_env:
        if from_env not in os.environ:
            # An unset var must never quietly become an empty secret; that would
            # "succeed" and break the pipeline somewhere else entirely.
            raise OpError(f"--from-env {from_env}: that environment variable is not set.")
        data = os.environ[from_env]
    elif from_file:
        path = Path(from_file).expanduser()
        try:
            data = _strip_one_newline(path.read_text())
        except OSError as exc:
            raise OpError(f"--from-file {from_file}: {exc}") from exc
    else:
        data = _strip_one_newline(sys.stdin.read())

    if not data:
        raise ValidationError(
            "the value is empty. Drone cannot store an empty secret (core.Secret.Validate "
            "rejects it), and an empty secret would read as 'set' while being useless."
        )
    return data


def refuse_value_projection(obj) -> None:
    """Refuse `--fields data` (and `--fields data.x`).

    The projector walks whatever keys are present, so this would happily emit
    `data: null` — which reads as "the secret is blank" and is a *lie* about a
    secret that is set. Refuse loudly instead of answering wrongly.
    """
    for field in obj.emitter.fields or []:
        if field.split(".", 1)[0] in ("data", "value"):
            raise ValidationError(
                f"--fields {field}: refused. {WRITE_ONLY} Project name/pull_request/"
                f"pull_request_push instead."
            )


def shape(sec: dict) -> dict:
    """Allowlist the fields we emit.

    An allowlist, not `del sec['data']`: this must not depend on the server
    continuing to blank the value, and `type` is decoded then discarded by Drone,
    so exposing it would invent a knob that does nothing.
    """
    if not isinstance(sec, dict):
        return {}
    out: dict = {}
    for key in ("name", "pull_request", "pull_request_push", "id", "repo_id", "namespace"):
        if key in sec:
            out[key] = sec[key]
    return out


# ---------------------------------------------------------------------------
# writes
# ---------------------------------------------------------------------------


def _body(
    client,
    *,
    name: str | None = None,
    data: str | None = None,
    pull_request: bool | None = None,
    pull_request_push: bool | None = None,
) -> dict:
    """The request body — with the value redacted when this is a dry run.

    This is the whole reason writes don't just hand `data` to the client: the
    dry-run interceptor in `client.py` prints the body verbatim and has no way to
    know which field is a secret. Redacting at the call site is the only place
    that knowledge exists. `None` fields are omitted so a PATCH never clobbers a
    flag the caller didn't mention.
    """
    body: dict = {}
    if name is not None:
        body["name"] = name
    if data is not None:
        body["data"] = REDACTED if getattr(client, "dry_run", False) else data
    if pull_request is not None:
        body["pull_request"] = pull_request
    if pull_request_push is not None:
        body["pull_request_push"] = pull_request_push
    return body


def upsert(
    client,
    base: str,
    name: str,
    *,
    data: str,
    pull_request: bool | None,
    pull_request_push: bool | None,
) -> tuple[dict, str]:
    """Create-or-update, returning (secret, "created"|"updated").

    Drone has no PUT and no upsert, so callers are forced into a probe-and-branch
    dance. We probe with GET (reads still execute under --dry-run, so the printed
    verb is the real one) and then handle the loser of each race: the secret can
    be deleted between probe and PATCH (-> 404), or created between probe and
    POST. Note the second case surfaces as **400, not 409** — Drone has no
    optimistic locking and maps uniqueness collisions onto its validation status.
    """
    try:
        client.get(f"{base}/{name}")
        exists = True
    except NotFoundError:
        exists = False

    patch_body = _body(
        client, data=data, pull_request=pull_request, pull_request_push=pull_request_push
    )
    post_body = _body(
        client,
        name=name,
        data=data,
        pull_request=pull_request,
        pull_request_push=pull_request_push,
    )

    if exists:
        try:
            return client.patch(f"{base}/{name}", json=patch_body) or {}, "updated"
        except NotFoundError:
            return client.post(base, json=post_body) or {}, "created"
    try:
        return client.post(base, json=post_body) or {}, "created"
    except (ValidationError, ConflictError):
        return client.patch(f"{base}/{name}", json=patch_body) or {}, "updated"


def emit_written(obj, sec: dict, *, name: str, action: str, pull_request: bool | None) -> None:
    out = shape(sec) or {"name": name}
    out.setdefault("name", name)
    out["action"] = action
    out["note"] = WRITE_ONLY
    if out.get("pull_request") or pull_request:
        out["warning"] = PR_WARNING
    obj.emitter.emit(out)


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


@app.command("ls")
def ls(
    ctx: typer.Context,
    repo: str = typer.Option(None, "--repo", "-r", help="owner/name (or set a sticky context)."),
) -> None:
    """List this repo's secrets — names and flags only, never values.

    This is the whole audit surface: which secrets exist, and which are exposed
    to pull-request builds. Use it to spot a repo missing a secret its pipeline
    needs; you cannot use it to compare values against anything.
    """
    obj = ctx_obj(ctx)
    refuse_value_projection(obj)
    client = obj.client()
    slug = need_repo(obj, repo)
    # No `paginate`: the secrets handler has no pagination at all and returns the
    # full array. Paging it would send page/per_page that the server ignores.
    rows = client.get(f"repos/{slug}/secrets") or []
    obj.emitter.emit([shape(s) for s in rows], columns=COLUMNS)


@app.command("get")
def get(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Secret name."),
    repo: str = typer.Option(None, "--repo", "-r", help="owner/name."),
) -> None:
    """Show one secret's metadata. THE VALUE IS NOT RETURNED — and never can be.

    Drone omits the value from every response by design, so this answers "does it
    exist, and is it exposed to PRs?" and nothing more. `data` is emitted as
    `null` meaning *unreadable*, not empty. If you need the value, you must
    already have it: overwrite with `drone-cli secret set`.
    """
    obj = ctx_obj(ctx)
    refuse_value_projection(obj)
    client = obj.client()
    slug = need_repo(obj, repo)
    sec = client.get(f"repos/{slug}/secrets/{validate_name(name)}")
    out = shape(sec)
    out["data"] = None
    out["note"] = WRITE_ONLY
    if out.get("pull_request"):
        out["warning"] = PR_WARNING
    obj.emitter.emit(out)


@app.command("set")
def set_(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Secret name ([A-Za-z0-9._-]+)."),
    repo: str = typer.Option(None, "--repo", "-r", help="owner/name."),
    value: str = typer.Option(
        None, "--value", help="The value, inline. Visible in `ps` and shell history — prefer --from-env."
    ),
    from_env: str = typer.Option(
        None, "--from-env", help="Read the value from this environment variable. Safest source."
    ),
    from_file: str = typer.Option(None, "--from-file", help="Read the value from this file."),
    from_stdin: bool = typer.Option(False, "--from-stdin", help="Read the value from stdin."),
    pull_request: bool = typer.Option(
        None,
        "--pull-request/--no-pull-request",
        help="Expose to pull_request builds. SECURITY BOUNDARY — any PR author can print it. Default: off.",
    ),
    pull_request_push: bool = typer.Option(
        None,
        "--pull-request-push/--no-pull-request-push",
        help="Expose to pushes to a PR branch. Same exposure caveat as --pull-request.",
    ),
) -> None:
    """Create or overwrite a secret. Idempotent — run it twice, same result.

        drone-cli secret set docker_password --from-env DOCKER_PW -r acme/api
        cat key.pem | drone-cli secret set ssh_key --from-stdin -r acme/api

    Why `set` and not add/update: Drone has no PUT and no upsert, so the raw API
    forces a create -> 404 -> patch dance. This probes and picks for you, and
    reports which happened as `action: created|updated`.

    The value comes from exactly one of --from-env (safest), --from-file,
    --from-stdin or --value; one trailing newline is stripped from file/stdin
    input, because `echo`-fed tokens with a stray "\\n" fail at build time in a
    way that never points back here.

    Notes that bite:
      * You cannot read the value back afterwards. Ever. Keep your own copy.
      * A rename is impossible (delete + recreate needs the value you can't read).
      * `--dry-run` prints the request with the value replaced by ***REDACTED***.
      * `--pull-request` is a real security boundary, not a convenience flag.
    """
    obj = ctx_obj(ctx)
    refuse_value_projection(obj)
    client = obj.client()
    slug = need_repo(obj, repo)
    key = validate_name(name)
    data = read_value(value, from_env, from_file, from_stdin)

    sec, action = upsert(
        client,
        f"repos/{slug}/secrets",
        key,
        data=data,
        pull_request=pull_request,
        pull_request_push=pull_request_push,
    )
    emit_written(obj, sec, name=key, action=action, pull_request=pull_request)


@app.command("rm")
def rm(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Secret name to delete."),
    repo: str = typer.Option(None, "--repo", "-r", help="owner/name."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Delete a secret.

    Irreversible in a way most deletes are not: you cannot read the value first,
    so if you do not already have it stored elsewhere, it is gone. Any pipeline
    referencing it starts failing on the next build with an empty variable rather
    than a clear error.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    slug = need_repo(obj, repo)
    key = validate_name(name)
    if not yes:
        typer.confirm(f"Delete secret {key!r} from {slug}? The value cannot be recovered.", abort=True)
    client.delete(f"repos/{slug}/secrets/{key}")
    obj.emitter.emit({"status": "deleted", "repo": slug, "name": key})
