"""`drone-cli orgsecret` — namespace-scoped secrets, shared by every repo in an org.

Same verbs and the same write-only rule as `drone-cli secret` (see that module —
the value plumbing and the redaction chokepoint live there and are imported, not
copied). Two things genuinely differ, and both are traps:

* **The URL tree.** Org secrets live at ``/api/secrets/{namespace}``. The
  intuitive ``/api/orgs/{ns}/secrets`` **404s** — verified live, even against a
  real org. Expect to re-learn this; the intuitive path is the wrong one.
* **The ACL and the blast radius.** Reading needs org *membership*, writing needs
  org *admin*, and the secret is visible to every repo in the namespace — so a
  mistake here is wider than a repo secret, not narrower.

Drone Cloud disables this group entirely (-> 501 -> exit 8). The published
``drone/drone:2`` image does not: org secrets returned 200 live.
"""

from __future__ import annotations

import typer
from agentcli.errors import ConfigError

from .secret import (
    COLUMNS,
    PR_WARNING,
    WRITE_ONLY,
    emit_written,
    read_value,
    refuse_value_projection,
    shape,
    upsert,
    validate_name,
)
from ._shared import ctx_obj

app = typer.Typer(no_args_is_help=True)


def need_org(obj, owner: str | None) -> str:
    """Resolve the target namespace, or explain how to set one.

    Order: explicit --org > sticky context `owner` > the namespace half of a
    sticky context `repo` > error. The last rung matters: someone who set
    `context set --repo acme/api` has already said "I am working in acme", and
    making them repeat it would be pedantry.
    """
    ctxvals = obj.config.context or {}
    ns = owner or ctxvals.get("owner")
    if not ns:
        repo = ctxvals.get("repo") or ""
        if "/" in repo:
            ns = repo.split("/", 1)[0]
    if not ns:
        raise ConfigError(
            "no org. Pass --org NAMESPACE, or set a sticky default with "
            "`drone-cli context set --owner NAMESPACE` (or --repo owner/name)."
        )
    ns = ns.strip("/")
    if "/" in ns:
        raise ConfigError(
            f"--org takes a namespace (the owner half), not a repo slug — got {ns!r}. "
            f"Did you mean `drone-cli secret` (repo-scoped) instead?"
        )
    return ns


@app.command("ls")
def ls(
    ctx: typer.Context,
    owner: str = typer.Option(None, "--org", help="Namespace, e.g. acme. Defaults from your sticky context."),
) -> None:
    """List an org's secrets — names and flags only, never values.

    These apply to every repo in the namespace, which is exactly why the list is
    worth auditing: a `pull_request: true` org secret is exposed to PR builds in
    *all* of them.
    """
    obj = ctx_obj(ctx)
    refuse_value_projection(obj)
    client = obj.client()
    ns = need_org(obj, owner)
    # /api/secrets/{ns} -- NOT /api/orgs/{ns}/secrets, which 404s. No pagination
    # on this handler; it returns the full array.
    rows = client.get(f"secrets/{ns}") or []
    obj.emitter.emit([shape(s) for s in rows], columns=COLUMNS)


@app.command("get")
def get(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Secret name."),
    owner: str = typer.Option(None, "--org", help="Namespace, e.g. acme."),
) -> None:
    """Show one org secret's metadata. THE VALUE IS NOT RETURNED — and never can be.

    `data: null` means *unreadable*, not empty. Reading requires org membership;
    a 404 here can therefore mean "no such secret" *or* "not your org".
    """
    obj = ctx_obj(ctx)
    refuse_value_projection(obj)
    client = obj.client()
    ns = need_org(obj, owner)
    sec = client.get(f"secrets/{ns}/{validate_name(name)}")
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
    owner: str = typer.Option(None, "--org", help="Namespace, e.g. acme."),
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
        help="Expose to pull_request builds in EVERY repo of the org. SECURITY BOUNDARY. Default: off.",
    ),
    pull_request_push: bool = typer.Option(
        None,
        "--pull-request-push/--no-pull-request-push",
        help="Expose to pushes to a PR branch. Same org-wide exposure caveat.",
    ),
) -> None:
    """Create or overwrite an org secret. Idempotent — run it twice, same result.

        drone-cli orgsecret set docker_password --from-env DOCKER_PW --org acme

    Same upsert as `drone-cli secret set` (Drone has no PUT: this probes, then
    POSTs or PATCHes, and reports `action: created|updated`) against the org
    tree at /api/secrets/{namespace}. Requires **org admin**; a 404 on the write
    usually means the namespace is not one you administer, not that it is missing.

    The value is write-only afterwards — you can never read it back, and
    `--dry-run` prints it as ***REDACTED***. `--pull-request` here exposes the
    secret to PR builds across every repo in the namespace at once.
    """
    obj = ctx_obj(ctx)
    refuse_value_projection(obj)
    client = obj.client()
    ns = need_org(obj, owner)
    key = validate_name(name)
    data = read_value(value, from_env, from_file, from_stdin)

    sec, action = upsert(
        client,
        f"secrets/{ns}",
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
    owner: str = typer.Option(None, "--org", help="Namespace, e.g. acme."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Delete an org secret.

    Wider blast radius than it looks: every repo in the namespace loses it at
    once, and their next builds fail with an empty variable rather than a clear
    error. You cannot read the value first, so there is no undo.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    ns = need_org(obj, owner)
    key = validate_name(name)
    if not yes:
        typer.confirm(
            f"Delete org secret {key!r} from {ns}? Every repo in {ns} loses it, and the "
            f"value cannot be recovered.",
            abort=True,
        )
    client.delete(f"secrets/{ns}/{key}")
    obj.emitter.emit({"status": "deleted", "org": ns, "name": key})
