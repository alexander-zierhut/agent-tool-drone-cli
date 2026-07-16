"""`drone-cli template` — namespaced pipeline templates.

Two facts shape this module, and the second one is a trap for anyone who read
`secret.py` first:

* **Templates are namespaced, like org secrets — not repo-scoped.** They live at
  ``/api/templates/{namespace}``. There is no bare collection: ``POST
  /api/templates`` returns **405 Method Not Allowed**, not 404 — verified live.
  A 405 reads as "wrong verb, right URL" and sends you hunting for a POST that
  works; the truth is the URL is incomplete.

* **A template's `data` IS returned.** Verified live::

      [{"id":1,"name":"t.yml","namespace":"acme","data":"kind: pipeline"}]

  This is the exact opposite of secrets, whose value is write-only and can never
  be read back. If you learned "Drone blanks the payload on read" from
  `secret.py`, that lesson does **not** transfer here — templates round-trip.
  Concretely: `template get` really can show you the YAML, `--out` really can
  reconstruct the file on disk, and there is no redaction chokepoint in this
  module because there is nothing to redact. (Which cuts the other way too: a
  template is readable by anyone who can read the namespace, so it is not a
  place to hide a credential — use a secret.)

THE ERGONOMIC WIN — `--from-file`. The API wants an entire YAML document
JSON-escaped into a single `data` string. Doing that by hand (or by prompting a
model to emit correctly-escaped YAML-inside-JSON) is the most painful part of
this API and the easiest to get subtly wrong. `--from-file pipeline.yml` reads
the bytes and lets the JSON encoder escape them, which is always right.
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer
from agentcli.errors import OpError

from ..errors import ConflictError, NotFoundError, ValidationError
from ._shared import ctx_obj
from .orgsecret import need_org  # same --org > context.owner > context.repo's namespace rule

app = typer.Typer(no_args_is_help=True)

#: `data` is deliberately absent: it is a whole YAML document and would wreck a
#: table. `ls` reports size instead — see :func:`shape`.
COLUMNS = ["name", "namespace", "data_lines", "data_bytes"]

READABLE = (
    "unlike secrets, a template's `data` IS returned by the API — `template get NAME` shows "
    "the YAML and `--out PATH` writes it back to disk. A template is therefore NOT a place to "
    "put a credential: anyone who can read the namespace can read it."
)


# ---------------------------------------------------------------------------
# names and bodies
# ---------------------------------------------------------------------------


def validate_name(name: str) -> str:
    """Reject only what we *know* is broken.

    Deliberately NOT secret.py's ``NAME_RE``. That regex mirrors Drone's own
    ``core.Secret.Validate``; there is no verified equivalent for templates, so
    reusing it would invent a server rule and reject names the server may well
    accept. This checks the two things that are true regardless: a name is
    required, and a slash would silently re-point the URL at a different path
    segment rather than name a template.
    """
    n = (name or "").strip()
    if not n:
        raise ValidationError("a template name is required, e.g. deploy.yml.")
    if "/" in n:
        raise ValidationError(
            f"invalid template name {name!r}: a template name is a filename (deploy.yml), "
            f"not a path — a slash would change which URL is called."
        )
    return n


def read_data(from_file: str | None, from_stdin: bool) -> str:
    """Resolve the template body from exactly one source.

    Note what this does NOT do: it does not strip a trailing newline, and it
    does not call ``secret.read_value``, which does. That strip exists because a
    token with a stray "\\n" fails auth at build time — a secret-specific rule.
    A template is a *file*: it should land on the server byte-for-byte as it sits
    on disk, so that `template get --out` round-trips to an identical file.
    There is also no --from-env here: nobody keeps a YAML document in an
    environment variable, and offering it would imply this is secret-shaped.
    """
    chosen = [n for n, on in (("--from-file", bool(from_file)), ("--from-stdin", from_stdin)) if on]
    if not chosen:
        raise OpError(
            "no template body. Pass --from-file PATH (this is the point: it JSON-escapes the "
            "YAML for you) or --from-stdin."
        )
    if len(chosen) > 1:
        raise OpError(f"pass exactly one body source, got {', '.join(chosen)}.")

    if from_file:
        path = Path(from_file).expanduser()
        try:
            data = path.read_text()
        except OSError as exc:
            raise OpError(f"--from-file {from_file}: {exc}") from exc
    else:
        data = sys.stdin.read()

    if not data.strip():
        raise ValidationError(
            "the template body is empty. An empty template would store fine and then fail at "
            "build time, where the error points at the pipeline rather than at this command."
        )
    return data


def shape(tpl: dict, *, with_data: bool = True) -> dict:
    """Normalise one template, optionally dropping the body.

    `ls` drops it: a namespace of ten templates is ten full YAML documents, which
    is a wall of text for a human and a context-window bill for an agent, when
    the question `ls` answers is "what exists here?". The size fields are kept so
    "is this the empty one?" is still answerable without a second call.
    """
    if not isinstance(tpl, dict):
        return {}
    out: dict = {}
    for key in ("id", "name", "namespace", "created", "updated"):
        if key in tpl:
            out[key] = tpl[key]
    data = tpl.get("data")
    if isinstance(data, str):
        out["data_bytes"] = len(data.encode("utf-8"))
        out["data_lines"] = len(data.splitlines())
    if with_data and "data" in tpl:
        out["data"] = tpl["data"]
    return out


def _upsert(client, ns: str, name: str, data: str) -> tuple[dict, str]:
    """Create-or-update, returning (template, "created"|"updated").

    Same shape as ``secret.upsert`` and same reason — Drone has no PUT and no
    upsert — but not shared with it: that helper's signature is built around
    `pull_request` flags and its POST/PATCH bodies are secret-shaped. Handles
    both losers of the race: deleted between probe and PATCH (-> 404), created
    between probe and POST (-> **400**, since Drone has no 409 and maps
    uniqueness onto its validation status).
    """
    try:
        client.get(f"templates/{ns}/{name}")
        exists = True
    except NotFoundError:
        exists = False

    if exists:
        try:
            return client.patch(f"templates/{ns}/{name}", json={"data": data}) or {}, "updated"
        except NotFoundError:
            return client.post(f"templates/{ns}", json={"name": name, "data": data}) or {}, "created"
    try:
        return client.post(f"templates/{ns}", json={"name": name, "data": data}) or {}, "created"
    except (ValidationError, ConflictError):
        return client.patch(f"templates/{ns}/{name}", json={"data": data}) or {}, "updated"


def _fetch(client, ns: str, name: str) -> dict:
    """One template, by name.

    UNVERIFIED ROUTE — the spike confirmed `GET|POST /api/templates/{ns}` (200)
    and `POST /api/templates` (405), but never exercised
    `GET /api/templates/{ns}/{name}`. The docs list a "Template Info" verb and
    the sibling `/api/secrets/{ns}/{name}` tree is verified, so the item route
    almost certainly exists — but "almost certainly" is not what this file is
    for. So: try the item route, and if it 404s, fall back to the collection,
    which IS verified and returns `data`. That is correct either way — if the
    route is missing, the fallback answers; if the template is simply absent,
    the fallback finds nothing and we raise with a useful message. The only cost
    of being wrong is one extra GET on a miss.
    """
    try:
        got = client.get(f"templates/{ns}/{name}")
        if isinstance(got, dict):
            return got
        # A non-dict body means the path did not resolve to a template handler
        # (an unrouted /api path answers with plain text "404 page not found",
        # which client.py hands back as a string rather than exploding).
    except NotFoundError:
        pass

    for row in client.get(f"templates/{ns}") or []:
        if isinstance(row, dict) and row.get("name") == name:
            return row
    raise NotFoundError(
        f"no template {name!r} in namespace {ns!r}. List them: drone-cli template ls --org {ns}",
        detail={"namespace": ns, "name": name},
    )


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


@app.command("ls")
def ls(
    ctx: typer.Context,
    owner: str = typer.Option(None, "--org", "--namespace", help="Namespace, e.g. acme. Defaults from your sticky context."),
    with_data: bool = typer.Option(False, "--with-data", help="Include each template's full YAML body. Verbose by design."),
) -> None:
    """List a namespace's templates.

    Templates are namespaced, not repo-scoped: this is every template available
    to every repo in the org.

    The body is omitted by default — ten templates is ten YAML documents, and the
    question here is "what exists?". `data_lines`/`data_bytes` are still reported
    so you can spot an empty one. Use `template get NAME` for one body, or
    `--with-data` for all of them.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    ns = need_org(obj, owner)
    # No `paginate`: like the secrets handler this returns the full array and
    # ignores page/per_page, so paging it would just send params into the void.
    rows = client.get(f"templates/{ns}") or []
    obj.emitter.emit([shape(t, with_data=with_data) for t in rows], columns=COLUMNS)


@app.command("get")
def get(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Template name, e.g. deploy.yml."),
    owner: str = typer.Option(None, "--org", "--namespace", help="Namespace, e.g. acme."),
    out: str = typer.Option(None, "--out", help="Write the YAML body to this file instead of embedding it. NOT --output (reserved global)."),
) -> None:
    """Show one template, including its YAML body.

    THE BODY IS RETURNED — templates are not secrets. If you came from
    `drone-cli secret get`, forget what it taught you: there is no write-only
    rule here, `data` is the real YAML, and it round-trips.

        drone-cli template get deploy.yml --org acme --out deploy.yml

    Note the flag is `--out`, NOT `--output`: `--output`/`-o` is a reserved
    global (the output *format*) that is stripped from the command line before
    this command ever sees it. The sibling OpenProject CLI shipped exactly that
    collision for four releases — the path was swallowed as a format, silently
    degraded to json, and the file landed in the working directory under the
    wrong name with exit 0.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    ns = need_org(obj, owner)
    key = validate_name(name)
    tpl = _fetch(client, ns, key)

    if not out:
        result = shape(tpl)
        result["note"] = READABLE
        obj.emitter.emit(result)
        return

    data = tpl.get("data") or ""
    if out == "-":
        # `-` conventionally means stdout, but stdout is the emitter's channel:
        # interleaving raw YAML with the JSON envelope would corrupt whatever is
        # parsing it. Refusing is better than writing a file literally named "-",
        # which is what treating it as a path would do.
        raise OpError(
            "--out - is not supported: stdout carries the machine-readable envelope, and "
            "interleaving raw YAML would break any parser reading it. Write to a file, or "
            "use `--fields data` to project just the body."
        )
    path = Path(out).expanduser()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)
    except OSError as exc:
        raise OpError(f"--out {out}: {exc}") from exc

    # The body is in the file now; echoing it here as well would double every
    # byte for no gain. Report where it went instead.
    result = shape(tpl, with_data=False)
    result["path"] = str(path)
    result["action"] = "written"
    obj.emitter.emit(result)


@app.command("add")
def add(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Template name. Convention is a filename, e.g. deploy.yml."),
    owner: str = typer.Option(None, "--org", "--namespace", help="Namespace, e.g. acme."),
    from_file: str = typer.Option(None, "--from-file", help="Read the YAML body from this file. Handles the JSON escaping for you."),
    from_stdin: bool = typer.Option(False, "--from-stdin", help="Read the YAML body from stdin."),
) -> None:
    """Create a template from a YAML file.

        drone-cli template add deploy.yml --from-file ./deploy.yml --org acme
        cat deploy.yml | drone-cli template add deploy.yml --from-stdin --org acme

    Why --from-file is the whole point: the API wants the entire YAML document
    JSON-escaped into a single `data` string. Hand-escaping a multi-line document
    with quotes and colons in it is the most error-prone thing in this API, and a
    subtly wrong escape produces a template that stores fine and then fails at
    build time, where the error names the pipeline instead of this command.
    --from-file passes the bytes through the JSON encoder, which is always right.

    The body is stored verbatim — no trailing newline is stripped (that is a
    secret-value rule, and a template is a file).

    Fails if the name already exists (Drone has no upsert, and reports the
    collision as **400**, not 409). To overwrite, use `template update`; for a
    whole directory, `template push` picks create-or-update per file.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    ns = need_org(obj, owner)
    key = validate_name(name)
    data = read_data(from_file, from_stdin)

    tpl = client.post(f"templates/{ns}", json={"name": key, "data": data}) or {}
    result = shape(tpl, with_data=False) or {"name": key, "namespace": ns}
    result.setdefault("name", key)
    result.setdefault("namespace", ns)
    result["action"] = "created"
    result["note"] = READABLE
    obj.emitter.emit(result)


@app.command("update")
def update(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Existing template name."),
    owner: str = typer.Option(None, "--org", "--namespace", help="Namespace, e.g. acme."),
    from_file: str = typer.Option(None, "--from-file", help="Read the new YAML body from this file."),
    from_stdin: bool = typer.Option(False, "--from-stdin", help="Read the new YAML body from stdin."),
) -> None:
    """Replace a template's YAML body.

        drone-cli template update deploy.yml --from-file ./deploy.yml --org acme

    Sends `{"data": ...}` to PATCH /api/templates/{namespace}/{name}. The body
    is a full replacement, not a merge — whatever you pass becomes the template.

    UNVERIFIED: the exact PATCH body shape was not exercised during the live
    spike (only `POST|GET /api/templates/{ns}` were). `{"data": ...}` mirrors the
    create body minus the immutable name, and matches how the verified secrets
    tree PATCHes. If a server rejects it, that is the first thing to check —
    `drone-cli template update ... --dry-run` prints the exact request.

    Renaming is not possible here: `name` is the URL, so a "rename" is
    `template add` under the new name plus `template rm` of the old one. Unlike a
    secret, that is safe — you can read the body back first.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    ns = need_org(obj, owner)
    key = validate_name(name)
    data = read_data(from_file, from_stdin)

    tpl = client.patch(f"templates/{ns}/{key}", json={"data": data}) or {}
    result = shape(tpl, with_data=False) or {"name": key, "namespace": ns}
    result.setdefault("name", key)
    result.setdefault("namespace", ns)
    result["action"] = "updated"
    obj.emitter.emit(result)


@app.command("rm")
def rm(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Template name to delete."),
    owner: str = typer.Option(None, "--org", "--namespace", help="Namespace, e.g. acme."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Delete a template.

    The blast radius is the whole namespace: any pipeline in any repo that
    `load:`s this template starts failing on its next build, and nothing warns
    you which ones do — Drone tracks no reverse index from template to consumer.

    Unlike deleting a secret, this IS recoverable in principle: read the body
    first (`template get NAME --out backup.yml`) and you can put it back.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    ns = need_org(obj, owner)
    key = validate_name(name)
    if not yes:
        typer.confirm(
            f"Delete template {key!r} from namespace {ns}? Any pipeline in {ns} that loads it "
            f"will fail on its next build.",
            abort=True,
        )
    client.delete(f"templates/{ns}/{key}")
    obj.emitter.emit({"status": "deleted", "namespace": ns, "name": key})


@app.command("push")
def push(
    ctx: typer.Context,
    directory: str = typer.Argument(..., help="Directory of template files to upload."),
    owner: str = typer.Option(None, "--org", "--namespace", help="Namespace, e.g. acme."),
    pattern: str = typer.Option("*.yml", "--glob", help="Which files to push. Default *.yml; use *.star or *.jsonnet for those engines."),
) -> None:
    """Upload every *.yml in a directory as a template named after the file.

        drone-cli template push ./templates --org acme

    Each file is create-or-update (Drone has no PUT, so this probes and picks),
    so re-running it is idempotent and the whole directory is safe to keep in
    git as the source of truth. Each result reports `action: created|updated`.

    Not a sync: files deleted locally are NOT deleted server-side. Removing a
    template is a blast-radius decision (see `template rm`) and must stay
    explicit rather than fall out of a directory listing.

    `--dry-run` previews only the FIRST file: the dry-run interceptor aborts the
    run at the first write, by design, and this command does not defeat it.
    """
    obj = ctx_obj(ctx)
    client = obj.client()
    ns = need_org(obj, owner)

    root = Path(directory).expanduser()
    if not root.is_dir():
        raise OpError(f"{directory}: not a directory. `template push` takes a directory; "
                      f"for one file use `template add NAME --from-file {directory}`.")

    files = sorted(p for p in root.glob(pattern) if p.is_file())
    if not files:
        raise OpError(
            f"no files matching {pattern!r} in {directory}. Pass --glob to match a different "
            f"extension (e.g. --glob '*.star')."
        )

    results = []
    for path in files:
        data = path.read_text()
        if not data.strip():
            # Skipping beats storing an empty template that fails later at build
            # time, and beats aborting a bulk run over one stray file.
            results.append({"name": path.name, "namespace": ns, "action": "skipped",
                            "reason": "file is empty"})
            continue
        tpl, action = _upsert(client, ns, validate_name(path.name), data)
        row = shape(tpl, with_data=False) or {}
        row.setdefault("name", path.name)
        row.setdefault("namespace", ns)
        row["action"] = action
        row["source"] = str(path)
        results.append(row)

    obj.emitter.emit(results, columns=["name", "namespace", "action", "source"])
