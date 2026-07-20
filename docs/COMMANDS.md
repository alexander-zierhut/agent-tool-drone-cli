# Command reference

_Auto-generated from the CLI (`python scripts/gen_docs.py`)._

_Every command also accepts `--output/-o` (json\|table\|markdown\|csv), `--format/-f`, `--fields`/`--columns`, `--dry-run`, `--stream` and `--no-context`. Those are **stripped from argv before parsing**, so they work anywhere on the line — before or after the subcommand. `--profile/-p` and `--no-color` are ordinary root options and must therefore come **before** the subcommand (`drone-cli -p prod build ls`, not `drone-cli build ls -p prod`)._

## Groups

- [`auth`](#auth) — Log in, log out, inspect credentials.
- [`build`](#build) — Builds: list, inspect, run, restart, cancel, wait.
- [`context`](#context) — Sticky session defaults (repo, etc.) reused across commands.
- [`cron`](#cron) — Scheduled builds — with a guard for Drone's seconds-first cron.
- [`deploy`](#deploy) — Deployments: what is on prod, and what got it there.
- [`guide`](#guide) — Built-in operating guide — how to use this CLI without external docs.
- [`install`](#install) — Integrate with other tools (e.g. `install claude`).
- [`log`](#log) — Build logs — including just the failing step.
- [`orgsecret`](#orgsecret) — Organisation secrets, shared across a namespace's repos.
- [`promote`](#promote) — Promote a commit/build to a target (default: prod).
- [`raw`](#raw) — Escape hatch: call any API endpoint directly.
- [`repo`](#repo) — Repositories: list, enable, sync, inspect.
- [`report`](#report) — Report a bug or missing feature — prints this tool's repo and a pre-filled issue link (offline, no token).
- [`secret`](#secret) — Repository secrets (values are write-only).
- [`server`](#server) — Server version, health, queue — and `server doctor`.
- [`settings`](#settings) — View & change CLI settings.
- [`template`](#template) — Pipeline templates (namespaced).
- [`user`](#user) — Your build feed, and user administration (admin only).
- [`wait`](#wait) — Wait for a commit's build to finish. `--commit HEAD` after a push.

## `auth`

### `drone-cli auth login`

Log in and store the token in your OS keyring.

Interactive when flags are omitted: asks for the server, then — knowing the
URL — tells you exactly where to get a token, rather than making you hunt.

| Option | Description |
| --- | --- |
| `--server`, `-s` | Drone server URL, e.g. https://drone.example.com. |
| `--token`, `-t` | API token. Get one at <server>/account. |
| `--name` | Profile name (for multiple servers). |
| `--insecure` | Skip TLS verification (self-signed certs). |

### `drone-cli auth logout`

Remove the stored token for a profile.

| Option | Description |
| --- | --- |
| `--name` | Profile to log out (default: the active one). |

### `drone-cli auth status`

Show the active profile and — importantly — WHICH token is actually in use.

The precedence is env > keyring > file, so an exported DRONE_TOKEN silently
overrides a keyring login. That is deliberate (CI depends on it) but
confusing exactly when you can least afford it, so this command always names
the backend that will actually speak.

### `drone-cli auth whoami`

Show the authenticated Drone user.

## `build`

### `drone-cli build approve`

Approve a blocked stage. Requires admin on most servers.

**Arguments:** `number` (required)

| Option | Description |
| --- | --- |
| `--stage` | Stage number to approve (1-based). **(required)** |
| `--repo`, `-r` | owner/name. |

### `drone-cli build cancel`

Cancel a running build.

**Arguments:** `number` (required)

| Option | Description |
| --- | --- |
| `--repo`, `-r` | owner/name. |

### `drone-cli build decline`

Decline a blocked stage.

**Arguments:** `number` (required)

| Option | Description |
| --- | --- |
| `--stage` | Stage number to decline (1-based). **(required)** |
| `--repo`, `-r` | owner/name. |

### `drone-cli build info`

Show one build, including its stages and steps.

One GET returns the whole tree — stages[] with their steps[] embedded.

**Arguments:** `number` (optional)

| Option | Description |
| --- | --- |
| `--repo`, `-r` | owner/name. |
| `--commit`, `-c` | Address by commit instead (accepts HEAD). |
| `--event` | With --commit: which build to pick. Default: newest. |
| `--links` | Include derived commit/repo web URLs. |

### `drone-cli build ls`

List builds, newest first.

| Option | Description |
| --- | --- |
| `--repo`, `-r` | owner/name (or set a sticky context). |
| `--commit`, `-c` | Only builds for this commit (accepts HEAD). |
| `--branch`, `-b` | Filter by target branch. |
| `--event` | push \| pull_request \| tag \| promote \| rollback \| cron \| custom. |
| `--status` | Filter by status, e.g. failure. |
| `--limit`, `-n` | Max builds (0 = as many as we can page). |
| `--links` | Include derived commit/repo web URLs. |

### `drone-cli build restart`

Restart a build. Creates a NEW build number — it does not resume the old one.

**Arguments:** `number` (required)

| Option | Description |
| --- | --- |
| `--repo`, `-r` | owner/name. |

### `drone-cli build run`

Trigger a build.

NOTE: API-triggered builds have event=`custom`, not `push`. If your pipeline
has `trigger: event: [push]`, it will not run — that is a pipeline config
issue, not a CLI bug, and it surprises everyone exactly once.

| Option | Description |
| --- | --- |
| `--repo`, `-r` | owner/name. |
| `--branch`, `-b` | Branch to build (default: the repo's default). |
| `--commit`, `-c` | Specific commit to build. |
| `--param`, `-P` | KEY=VALUE build parameter (repeatable). |

## `context`

### `drone-cli context clear`

Clear the active context entirely. Saved contexts are untouched.

### `drone-cli context list`

List saved contexts.

### `drone-cli context rm`

Delete a saved context. Does not touch the active one, even if it came from here.

**Arguments:** `name` (required)

### `drone-cli context save`

Save the active context under a name for later reuse.

**Arguments:** `name` (required)

### `drone-cli context set`

Set/merge sticky defaults. Applies to later commands' matching options.

    drone-cli context set --repo octocat/hello-world

Then `drone-cli build ls` behaves like `drone-cli build ls --repo octocat/hello-world`.

| Option | Description |
| --- | --- |
| `--repo`, `-r` | Default repo, as an owner/name slug (not two keys, not an id). |
| `--owner` | Default namespace for org-scoped commands (`orgsecret --org`, templates). |
| `--branch`, `-b` | Default branch, e.g. main. |
| `--set` | Generic key=value (repeatable). Must be a known key. |

### `drone-cli context show`

Show the active context — each value, and where it came from.

Run this FIRST whenever output looks wrongly scoped: this is implicit state
that changes results, and nothing echoes it back on a normal command.

Every key is reported as `{"value": ..., "from": ...}` — `from` is `saved`
for anything set with `context set`, i.e. everything, for now. Read `applies`
before believing any of it: `--no-context` suspends the whole context for one
command, and then these values are saved but NOT in force.

### `drone-cli context unset`

Remove one key from the active context.

**Arguments:** `key` (required)

### `drone-cli context use`

Load a saved context as the active one. Replaces the active context wholesale.

**Arguments:** `name` (required)

## `cron`

### `drone-cli cron add`

Create a cron job. Prints the next 5 fire times it will produce.

    drone-cli cron add nightly --at '3am daily'
    drone-cli cron add nightly --expr '0 0 3 * * *'      # identical, 6-field
    drone-cli cron add nightly --expr '0 3 * * *'        # REFUSED: 5-field

Two things to know:
  * The expression is SECONDS-FIRST. `0 3 * * *` is not a syntax error to
    Drone — it means second=0 minute=3 hour=*, i.e. hourly at :03.
  * The cron's own `event` is forced to `push` server-side, but the BUILDS it
    creates carry `event=cron`. A pipeline gated on `event: [push]` will not
    run from a cron; gate on `cron` (or both).

**Arguments:** `name` (required)

| Option | Description |
| --- | --- |
| `--repo`, `-r` | owner/name. |
| `--expr` | 6-field expression: SECOND minute hour dom month dow. A 5-field crontab line is REFUSED. |
| `--at` | Plain English, e.g. '3am daily', 'every monday at 9am'. |
| `--every` | An interval, e.g. 15m, 1h. |
| `--preset` | One of: daily, every-15m, every-5m, hourly, midnight, monthly, nightly, weekly, workdays, yearly. |
| `--branch`, `-b` | Branch to build (default: the repo's default branch). |

### `drone-cli cron exec`

Run a cron NOW, off-schedule, and report the build it created.

Builds the current HEAD of the cron's branch, with `event=cron` — the same
shape the schedule would have produced, so this is how you test a cron
without waiting for 03:00.

The response carries the created build, which the official drone-go client
literally throws away (`c.post(uri, nil, nil)`); the build number is right
here in `number`.

**Arguments:** `name` (required)

| Option | Description |
| --- | --- |
| `--repo`, `-r` | owner/name. |

### `drone-cli cron get`

Show one cron, plus what its schedule actually means.

`next_fire_times` is computed locally from `expr` — it is the only way to see
more than one step ahead, since the server keeps a single `next`.

**Arguments:** `name` (required)

| Option | Description |
| --- | --- |
| `--repo`, `-r` | owner/name. |

### `drone-cli cron ls`

List the repo's cron jobs.

Listing crons needs **write** access to the repo — a 403 here means your
token is read-only, NOT that the repo has no crons. The endpoint returns the
whole array in one response; there is no pagination.

| Option | Description |
| --- | --- |
| `--repo`, `-r` | owner/name (or set a sticky context). |
| `--disabled`, `--enabled` | Only disabled (or only enabled) crons. |
| `--preview` | Also compute the next 5 fire times locally for each. |

### `drone-cli cron next`

Preview a schedule WITHOUT creating anything. No server call.

The API cannot do this at all: `next` only exists once a cron is persisted,
and it is one step deep. Use this to check an expression means what you think
before it becomes a job that quietly runs 24x too often.

    drone-cli cron next --expr '0 0 3 * * *'      # daily at 03:00
    drone-cli cron next --at '3am daily'

| Option | Description |
| --- | --- |
| `--expr` | 6-field expression: second minute hour dom month dow. |
| `--at` | Plain English, e.g. '3am daily', 'every monday at 9am'. |
| `--every` | An interval, e.g. 15m, 1h, 30s. |
| `--preset` | One of: daily, every-15m, every-5m, hourly, midnight, monthly, nightly, weekly, workdays, yearly. |
| `--count`, `-n` | How many fire times to show. |

### `drone-cli cron rm`

Delete a cron job. Irreversible — recreating it resets id and run history.

To pause one instead, `cron update NAME --disable` keeps the row.

**Arguments:** `name` (required)

| Option | Description |
| --- | --- |
| `--repo`, `-r` | owner/name. |
| `--yes`, `-y` | Confirm. Required when not on a TTY. |

### `drone-cli cron update`

Change a cron — honestly.

**The server's PATCH only understands `{branch, target, disabled}`.** Send it
a new `expr` or `name` and it decodes them, throws them away, and returns
**200 with the object unchanged**. (It also ignores JSON decode errors
outright, so a malformed body is a 200 no-op.) Every client that trusts that
200 reports a schedule change that did not happen.

So this command does two different things:

  * `--branch` / `--disable` / `--enable` -> a real PATCH, and we diff the
    response. If the server did not actually apply it, you get an error, not
    a green tick.
  * `--expr` / `--at` / `--every` / `--preset` / `--rename` -> **refused by
    default**, because the only way to do them is DELETE + POST. That is not
    an "update": it mints a new id, drops `prev` history, and — since the two
    calls are not atomic — leaves the repo with NO cron at all if the POST
    fails. Pass `--recreate` to say you accept that. Refusing to guess here
    is deliberate: silently destroying and rebuilding a schedule behind an
    innocuous verb is exactly the kind of surprise this CLI exists to avoid.

**Arguments:** `name` (required)

| Option | Description |
| --- | --- |
| `--repo`, `-r` | owner/name. |
| `--expr` | New 6-field expression. PATCH cannot do this — needs --recreate. |
| `--at` | New schedule in plain English. Needs --recreate. |
| `--every` | New interval. Needs --recreate. |
| `--preset` | New preset schedule. Needs --recreate. |
| `--branch`, `-b` | New branch to build. PATCH supports this. |
| `--disable`, `--enable` | Pause or resume the cron. PATCH supports this. |
| `--rename` | New name. PATCH cannot do this — needs --recreate. |
| `--recreate` | Allow DELETE+POST to change expr/name. Resets id and prev history. |

## `deploy`

### `drone-cli deploy ls`

Deployment history — every promote/rollback, newest first.

| Option | Description |
| --- | --- |
| `--repo`, `-r` | owner/name. |
| `--to`, `-t` | Only this target. |
| `--limit`, `-n` | Max rows. |

### `drone-cli deploy of`

Has this commit been promoted — and where to?

    drone-cli deploy of --commit HEAD
    drone-cli deploy of --commit HEAD --to prod

Also answers the follow-up an agent always needs: *is it still live, or has
something newer replaced it?*

| Option | Description |
| --- | --- |
| `--repo`, `-r` | owner/name. |
| `--commit`, `-c` | Commit to ask about (default: HEAD). |
| `--to`, `-t` | Only this target. |

### `drone-cli deploy status`

Which commit is currently deployed to a target?

    drone-cli deploy status              # what is on prod
    drone-cli deploy status --to staging

Defined as the newest **successful** promote or rollback to that target. A
failed promote deployed nothing, and reporting its commit as live would be
the most dangerous wrong answer this tool could give.

| Option | Description |
| --- | --- |
| `--repo`, `-r` | owner/name. |
| `--to`, `-t` | Target. Default: your promote_target setting (prod). |

## `guide`

### `drone-cli guide`

Built-in operating guide — how to use this CLI without external docs.

**Arguments:** `topic` (optional)

## `install`

### `drone-cli install claude`

Register this CLI with Claude Code as a Skill so Claude auto-uses it.

Writes ~/.claude/skills/drone-ci/SKILL.md (idiomatic discovery). Claude then
invokes it whenever you mention Drone CI. Reversible with --uninstall.

| Option | Description |
| --- | --- |
| `--project` | Install into ./.claude (this repo) instead of ~/.claude. |
| `--memory` | Also add a one-line hint to ~/.claude/CLAUDE.md. |
| `--force` | Install even if Claude Code isn't detected. |
| `--uninstall` | Remove the skill (and memory hint). |
| `--print` | Print the SKILL.md that would be written and exit. |

## `log`

### `drone-cli log failed`

Print the log of the failing step — and nothing else.

    drone-cli log failed --commit HEAD
    drone-cli log failed 42 --tail 100

The one command to reach for when CI is red. Resolves the build, finds which
step actually failed, and prints only its tail.

**Arguments:** `number` (optional)

| Option | Description |
| --- | --- |
| `--repo`, `-r` | owner/name. |
| `--commit`, `-c` | Address by commit (accepts HEAD). |
| `--event` | With --commit: which build to pick. |
| `--tail` | Lines of context from the end of each failing step. |
| `--grep` | Only lines containing this. |
| `--all` | Every failing step, not just the first. |

### `drone-cli log view`

Print one step's log.

OUTPUT IS TEXT, NOT JSON — the one place this CLI breaks the JSON contract,
because logs are prose. Use --raw for the structured envelope.

**Arguments:** `number` (optional)

| Option | Description |
| --- | --- |
| `--repo`, `-r` | owner/name. |
| `--commit`, `-c` | Address by commit (accepts HEAD). |
| `--stage` | Stage number (1-based ordinal, not an id). |
| `--step` | Step number (1-based ordinal, not a name). **(required)** |
| `--tail` | Only the last N lines. |
| `--grep` | Only lines containing this (case-insensitive). |
| `--raw` | Emit the {pos,out,time} envelope as JSON instead of text. |

## `orgsecret`

### `drone-cli orgsecret get`

Show one org secret's metadata. THE VALUE IS NOT RETURNED — and never can be.

`data: null` means *unreadable*, not empty. Reading requires org membership;
a 404 here can therefore mean "no such secret" *or* "not your org".

**Arguments:** `name` (required)

| Option | Description |
| --- | --- |
| `--org` | Namespace, e.g. acme. |

### `drone-cli orgsecret ls`

List an org's secrets — names and flags only, never values.

These apply to every repo in the namespace, which is exactly why the list is
worth auditing: a `pull_request: true` org secret is exposed to PR builds in
*all* of them.

| Option | Description |
| --- | --- |
| `--org` | Namespace, e.g. acme. Defaults from your sticky context. |

### `drone-cli orgsecret rm`

Delete an org secret.

Wider blast radius than it looks: every repo in the namespace loses it at
once, and their next builds fail with an empty variable rather than a clear
error. You cannot read the value first, so there is no undo.

**Arguments:** `name` (required)

| Option | Description |
| --- | --- |
| `--org` | Namespace, e.g. acme. |
| `--yes`, `-y` | Skip confirmation. |

### `drone-cli orgsecret set`

Create or overwrite an org secret. Idempotent — run it twice, same result.

    drone-cli orgsecret set docker_password --from-env DOCKER_PW --org acme

Same upsert as `drone-cli secret set` (Drone has no PUT: this probes, then
POSTs or PATCHes, and reports `action: created|updated`) against the org
tree at /api/secrets/{namespace}. Requires **org admin**; a 404 on the write
usually means the namespace is not one you administer, not that it is missing.

The value is write-only afterwards — you can never read it back, and
`--dry-run` prints it as ***REDACTED***. `--pull-request` here exposes the
secret to PR builds across every repo in the namespace at once.

**Arguments:** `name` (required)

| Option | Description |
| --- | --- |
| `--org` | Namespace, e.g. acme. |
| `--value` | The value, inline. Visible in `ps` and shell history — prefer --from-env. |
| `--from-env` | Read the value from this environment variable. Safest source. |
| `--from-file` | Read the value from this file. |
| `--from-stdin` | Read the value from stdin. |
| `--pull-request`, `--no-pull-request` | Expose to pull_request builds in EVERY repo of the org. SECURITY BOUNDARY. Default: off. |
| `--pull-request-push`, `--no-pull-request-push` | Expose to pushes to a PR branch. Same org-wide exposure caveat. |

## `promote`

### `drone-cli promote`

Promote a commit/build to a target (default: prod).

**Arguments:** `number` (optional)

| Option | Description |
| --- | --- |
| `--repo`, `-r` | owner/name. |
| `--commit`, `-c` | Promote THIS commit's build (accepts HEAD). |
| `--to`, `-t` | Target environment. Default: your `promote_target` setting (ships as prod). |
| `--param`, `-P` | KEY=VALUE parameter (repeatable). |
| `--links` | Include derived commit/repo web URLs. |

## `raw`

### `drone-cli raw delete`

DELETE an endpoint. Usually returns an empty body (-> `null`), not an object.

**Arguments:** `path` (required)

| Option | Description |
| --- | --- |
| `--param`, `-P` | Query param key=value (repeatable). |

### `drone-cli raw get`

GET an endpoint and print whatever it returns, unmodified.

Every path is under <server>/api. `/version`, `/healthz` and `/varz` are NOT:
they are mounted on the WEB root, so `raw get` cannot reach them and
`GET /api/version` is a 404 rather than a redirect (it does not exist, despite
looking like it should). Use `drone-cli server version` for those instead.

Reads always execute — the global `--dry-run` only suppresses writes.

**Arguments:** `path` (required)

| Option | Description |
| --- | --- |
| `--param`, `-P` | Query param key=value (repeatable). |

### `drone-cli raw patch`

PATCH an endpoint with a partial JSON body.

Drone has no optimistic locking — there is nothing to fetch and echo back
first, and a concurrent write simply wins. Beware the silent drop: several
handlers decode only a fixed subset of the body (e.g. PATCH users takes only
admin/active and ignores email), so a rejected field still returns 200. Read
the object back to confirm the change landed.

**Arguments:** `path` (required)

| Option | Description |
| --- | --- |
| `--data`, `-d` | JSON request body. |
| `--data-file` | File containing the JSON body. |
| `--param`, `-P` | Query param key=value (repeatable). |

### `drone-cli raw post`

POST to an endpoint. Note many Drone writes take QUERY PARAMS, not a body.

`POST repos/o/n/builds --param branch=main` triggers a build; the body is
ignored there. Preview any write with a global `--dry-run`.

**Arguments:** `path` (required)

| Option | Description |
| --- | --- |
| `--data`, `-d` | JSON request body. |
| `--data-file` | File containing the JSON body. |
| `--param`, `-P` | Query param key=value (repeatable). |

## `repo`

### `drone-cli repo chown`

Take ownership of a repository — always to YOURSELF.

There is no target parameter: the API can only chown to the calling user. To
hand a repo to someone else, they must run this themselves.

What ownership means, and why you would want it: the owner's git token is
what Drone uses to clone, to fetch `.drone.yml` and to manage the webhook.
When the previous owner's token expires or they leave, every build for the
repo starts failing with SCM errors that name nobody. Chowning to a live user
fixes it — then `repo repair` to re-register the hook under the new owner.

**Arguments:** `slug_arg` (optional)

| Option | Description |
| --- | --- |
| `--repo`, `-r` | owner/name (or set a sticky context). |

### `drone-cli repo disable`

Deactivate a repository: stop building it.

Sets `active: false` and keeps everything else — settings, secrets and build
history all survive, and `repo enable` brings it back as it was.

**It does NOT remove the webhook from the git provider**, contrary to the
docs and to common belief; the handler has no hook service at all. The git
provider keeps POSTing to Drone and Drone keeps ignoring it. If you need the
hook gone, delete it in the git provider.

**Arguments:** `slug_arg` (optional)

| Option | Description |
| --- | --- |
| `--repo`, `-r` | owner/name (or set a sticky context). |

### `drone-cli repo enable`

Activate a repository: register its webhook and start building it.

**This silently makes you the repo's owner.** Enabling sets `repo.UserID` to
YOU, which moves every webhook registration, every clone and every
`.drone.yml` fetch onto YOUR git token — not the token of whoever set the
repo up before. Two consequences worth knowing before you run it:

  * builds break the day your git token is revoked or you leave;
  * re-enabling an already-enabled repo re-chowns it to you, so this is a
    quiet way to take a colleague's repo hostage by accident.

Other things that surprise people exactly once:

  * 404 → Drone has never synced this repo. Use `--sync`.
  * 402 → the repo limit on a licensed server is reached; nothing is wrong
    with your request, the server will not activate one more repo until
    another is disabled (see `/varz` for the live count).
  * Defaults applied on activation: `config_path=.drone.yml`, `timeout=60`.

**Arguments:** `slug_arg` (optional)

| Option | Description |
| --- | --- |
| `--repo`, `-r` | owner/name (or set a sticky context). |
| `--sync` | Sync from the git provider first — needed for a repo Drone has never seen. |

### `drone-cli repo info`

Show one repository, including your permissions on it.

This is the only repo endpoint that returns `permissions {read,write,admin}`
— use it to find out whether a write will be allowed before attempting it.

A 404 here means one of three different things: the repo does not exist, it
exists in the git provider but Drone has not synced it (`drone-cli repo
sync`), or you cannot see it. It does NOT mean the repo is merely disabled —
a disabled repo still answers 200 with `active: false`.

**Arguments:** `slug_arg` (optional)

| Option | Description |
| --- | --- |
| `--repo`, `-r` | owner/name (or set a sticky context). |
| `--links` | Include the repo's web URL (repo_url). |

### `drone-cli repo ls`

List repositories, as Drone sees them.

Default is **active repos only** — the ones Drone actually builds. `--all`
adds every repo it merely knows about from the last sync, which on a big org
is mostly noise.

    drone-cli repo ls                 # what am I building?
    drone-cli repo ls --latest        # ...and is any of it red?

`--latest` uses the undocumented `?latest=true`, which returns every repo
WITH its last build attached. That is one request for the whole fleet rather
than one per repo — use it instead of looping `build ls`.

A repo you just created in the git provider will not appear at all until
`drone-cli repo sync` runs.

| Option | Description |
| --- | --- |
| `--all` | Include repos that are NOT enabled in Drone (default: active only). |
| `--latest` | Attach each repo's latest build — a whole fleet's health in ONE request. |
| `--namespace` | Only repos in this org/owner. |
| `--search`, `-q` | Substring match on the slug. |
| `--links` | Include the repo's web URL (repo_url). |

### `drone-cli repo repair`

Re-create the git provider's webhook and re-sync the repo's metadata.

The fix for "pushes stopped triggering builds": the webhook is gone, or it
points at a hostname that no longer resolves from the git provider.

Two things to know before running it:

  * **repair APPENDS a hook, it does not replace the stale one.** The old,
    broken hook stays registered in the git provider. Repair a URL problem
    three times and the repo has three hooks, two of them dead; delete the
    stale ones by hand in the provider's UI.
  * It acts as the repo's OWNER, not as you — if the owner's git token is
    dead, repair fails no matter who runs it. Fix that with `repo chown`
    first.

The endpoint returns nothing, so the repo is re-read afterwards and reported:
a bare "success" here would prove nothing at all.

**Arguments:** `slug_arg` (optional)

| Option | Description |
| --- | --- |
| `--repo`, `-r` | owner/name (or set a sticky context). |

### `drone-cli repo sync`

Re-read the repository list from the git provider.

**A brand-new repo is invisible to Drone until this runs.** Drone does not
discover repos on its own and the git provider does not push the list to it,
so `repo info`/`repo enable` on a repo created five minutes ago return a
baffling 404 until you sync. If a repo "does not exist", sync first, then
look again.

This is also the command that proves the server's SCM link works: it is the
one call that must use the git token, so it is where a dead SCM link shows
up (as an auth error naming exactly that).

| Option | Description |
| --- | --- |
| `--links` | Include each repo's web URL (repo_url). |

### `drone-cli repo update`

Change a repository's settings — and verify the change actually landed.

    drone-cli repo update --repo octocat/hello-world --protected --timeout 90

**Why this command is not a thin PATCH.** `--timeout` and `--trusted` are
gated on *system* admin, not repo admin. For anyone else the server accepts
the request, answers **200 with the old values**, and warns nobody: the field
is dropped in silence. Believing that 200 is how an agent concludes "the
timeout is now 90" when it is still 60. So every field sent here is diffed
against the object that comes back, and a dropped one is a hard failure
(exit 7) that names the field.

`--trusted` grants pipelines privileged containers and host mounts — it is a
privilege escalation for anyone who can push a `.drone.yml`, not a build
tweak. So it is gated: you confirm at a prompt, or you pass
`--i-understand-this-grants-privileged-containers` when there is no TTY to
prompt at. It is never granted silently. (`--no-trusted` REVOKES the grant and
needs no gate — taking privilege away is not the dangerous direction.)

`--visibility` is validated locally because the server's own check is
commented out: a typo would be stored and returned 200 forever.

**Arguments:** `slug_arg` (optional)

| Option | Description |
| --- | --- |
| `--repo`, `-r` | owner/name (or set a sticky context). |
| `--timeout` | Build timeout in MINUTES (not seconds). SYSTEM-ADMIN only. |
| `--protected`, `--no-protected` | Require approval before a build runs. |
| `--trusted`, `--no-trusted` | DANGEROUS: lets pipelines run privileged containers. SYSTEM-ADMIN only. |
| `--visibility` | public \| private \| internal. Checked here — the server accepts typos. |
| `--config-path` | Pipeline file to read, e.g. .drone.yml. Wrong value = every build errors. |
| `--i-understand-this-grants-privileged-containers` | Confirm --trusted without a prompt. Required to grant it off a TTY; it is the only way to say yes non-interactively, and it must be typed deliberately. |

## `report`

### `drone-cli report`

Report a bug or missing feature — prints this tool's repo and a pre-filled issue link (offline, no token).

## `secret`

### `drone-cli secret get`

Show one secret's metadata. THE VALUE IS NOT RETURNED — and never can be.

Drone omits the value from every response by design, so this answers "does it
exist, and is it exposed to PRs?" and nothing more. `data` is emitted as
`null` meaning *unreadable*, not empty. If you need the value, you must
already have it: overwrite with `drone-cli secret set`.

**Arguments:** `name` (required)

| Option | Description |
| --- | --- |
| `--repo`, `-r` | owner/name. |

### `drone-cli secret ls`

List this repo's secrets — names and flags only, never values.

This is the whole audit surface: which secrets exist, and which are exposed
to pull-request builds. Use it to spot a repo missing a secret its pipeline
needs; you cannot use it to compare values against anything.

| Option | Description |
| --- | --- |
| `--repo`, `-r` | owner/name (or set a sticky context). |

### `drone-cli secret rm`

Delete a secret.

Irreversible in a way most deletes are not: you cannot read the value first,
so if you do not already have it stored elsewhere, it is gone. Any pipeline
referencing it starts failing on the next build with an empty variable rather
than a clear error.

**Arguments:** `name` (required)

| Option | Description |
| --- | --- |
| `--repo`, `-r` | owner/name. |
| `--yes`, `-y` | Skip confirmation. |

### `drone-cli secret set`

Create or overwrite a secret. Idempotent — run it twice, same result.

    drone-cli secret set docker_password --from-env DOCKER_PW -r acme/api
    cat key.pem | drone-cli secret set ssh_key --from-stdin -r acme/api

Why `set` and not add/update: Drone has no PUT and no upsert, so the raw API
forces a create -> 404 -> patch dance. This probes and picks for you, and
reports which happened as `action: created|updated`.

The value comes from exactly one of --from-env (safest), --from-file,
--from-stdin or --value; one trailing newline is stripped from file/stdin
input, because `echo`-fed tokens with a stray "\n" fail at build time in a
way that never points back here.

Notes that bite:
  * You cannot read the value back afterwards. Ever. Keep your own copy.
  * A rename is impossible (delete + recreate needs the value you can't read).
  * `--dry-run` prints the request with the value replaced by ***REDACTED***.
  * `--pull-request` is a real security boundary, not a convenience flag.

**Arguments:** `name` (required)

| Option | Description |
| --- | --- |
| `--repo`, `-r` | owner/name. |
| `--value` | The value, inline. Visible in `ps` and shell history — prefer --from-env. |
| `--from-env` | Read the value from this environment variable. Safest source. |
| `--from-file` | Read the value from this file. |
| `--from-stdin` | Read the value from stdin. |
| `--pull-request`, `--no-pull-request` | Expose to pull_request builds. SECURITY BOUNDARY — any PR author can print it. Default: off. |
| `--pull-request-push`, `--no-pull-request-push` | Expose to pushes to a PR branch. Same exposure caveat as --pull-request. |

## `server`

### `drone-cli server doctor`

Diagnose this server, this token and this SCM link — and NAME the failure.

Run this first when anything is confusing. Drone renders six unrelated
problems as one "401 Unauthorized"; each of these has a different fix:

  unreachable          the URL/DNS/TLS is wrong, or the server is down
  not-a-drone-server   something answered, but it isn't Drone (proxy? wrong port?)
  no-token             nothing is authenticating you at all
  bad-token            your token is wrong (they never expire — so it's wrong, not stale)
  not-admin            your token is fine; the route is behind AuthorizeAdmin
  scm-link-broken      YOUR TOKEN IS FINE. The *server's* login to Gitea/GitHub is
                       dead, and Drone reports that as HTTP 500 "Unauthorized"
  scm-quota-exhausted  the SCM's API budget ran out; syncs fail (as a 500, naturally)
  probe-failed         the CHECK broke, not (necessarily) the server. Says nothing
                       about the thing it was probing — read its `message`.

Also reports WHICH credential is in use: an exported $DRONE_TOKEN silently
overrides a keyring login, and the Drone runner injects DRONE_* into every
build step.

Read-only and side-effect free (nothing is POSTed), and it never exits
non-zero for a failed probe — a report IS the deliverable. Check `status` and
`problems` in the JSON, not the exit code.

### `drone-cli server queue`

Show work the server has not finished yet — `GET /api/queue`. ADMIN ONLY.

SHAPE TRAP: these are **stages** (`[]core.Stage`), not builds. One build with
three parallel stages appears three times; `build_id` is what ties them
together, and `number` is the stage's 1-based ordinal within its build, not a
build number.

A row sitting at `pending` with no `machine` means nothing has claimed it —
usually no runner is connected, or none matches its `os`/`arch`. There is no
way to check runners directly: `GET /api/nodes` does not exist (404), despite
the client libraries declaring it.

### `drone-cli server version`

Show the Drone server's version — `GET /version` on the WEB root.

NOT `/api/version`, which is a 404 and does not exist despite looking like it
should. Unauthenticated, so this answers even when your token is wrong: it is
the reachability/compat probe, not an auth check.

## `settings`

### `drone-cli settings path`

Print the config file path.

### `drone-cli settings set-format`

Set the default output format.

**Arguments:** `fmt` (required)

### `drone-cli settings set-promote-target`

Set the default promotion target (ships as `prod`).

**Arguments:** `target` (required)

### `drone-cli settings set-scm`

Set which URL shape to use for commit links.

Drone's `repo.scm` field is an empty string even on a fully synced repo
(verified live), so the provider cannot be detected — hence this setting.
Gitea/Forgejo/GitHub share `/commit/{sha}`; GitLab and Bitbucket differ.

**Arguments:** `flavour` (required)

### `drone-cli settings set-scm-base-url`

Override the base URL used to build commit links.

Only needed when the repo's own `link` is wrong or unreachable from where you
are (e.g. it points at an internal hostname).

**Arguments:** `url` (required)

### `drone-cli settings show`

Show every setting, its value, and where it came from.

## `template`

### `drone-cli template add`

Create a template from a YAML file.

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

**Arguments:** `name` (required)

| Option | Description |
| --- | --- |
| `--org`, `--namespace` | Namespace, e.g. acme. |
| `--from-file` | Read the YAML body from this file. Handles the JSON escaping for you. |
| `--from-stdin` | Read the YAML body from stdin. |

### `drone-cli template get`

Show one template, including its YAML body.

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

**Arguments:** `name` (required)

| Option | Description |
| --- | --- |
| `--org`, `--namespace` | Namespace, e.g. acme. |
| `--out` | Write the YAML body to this file instead of embedding it. NOT --output (reserved global). |

### `drone-cli template ls`

List a namespace's templates.

Templates are namespaced, not repo-scoped: this is every template available
to every repo in the org.

The body is omitted by default — ten templates is ten YAML documents, and the
question here is "what exists?". `data_lines`/`data_bytes` are still reported
so you can spot an empty one. Use `template get NAME` for one body, or
`--with-data` for all of them.

| Option | Description |
| --- | --- |
| `--org`, `--namespace` | Namespace, e.g. acme. Defaults from your sticky context. |
| `--with-data` | Include each template's full YAML body. Verbose by design. |

### `drone-cli template push`

Upload every *.yml in a directory as a template named after the file.

    drone-cli template push ./templates --org acme

Each file is create-or-update (Drone has no PUT, so this probes and picks),
so re-running it is idempotent and the whole directory is safe to keep in
git as the source of truth. Each result reports `action: created|updated`.

Not a sync: files deleted locally are NOT deleted server-side. Removing a
template is a blast-radius decision (see `template rm`) and must stay
explicit rather than fall out of a directory listing.

`--dry-run` previews only the FIRST file: the dry-run interceptor aborts the
run at the first write, by design, and this command does not defeat it.

**Arguments:** `directory` (required)

| Option | Description |
| --- | --- |
| `--org`, `--namespace` | Namespace, e.g. acme. |
| `--glob` | Which files to push. Default *.yml; use *.star or *.jsonnet for those engines. |

### `drone-cli template rm`

Delete a template.

The blast radius is the whole namespace: any pipeline in any repo that
`load:`s this template starts failing on its next build, and nothing warns
you which ones do — Drone tracks no reverse index from template to consumer.

Unlike deleting a secret, this IS recoverable in principle: read the body
first (`template get NAME --out backup.yml`) and you can put it back.

**Arguments:** `name` (required)

| Option | Description |
| --- | --- |
| `--org`, `--namespace` | Namespace, e.g. acme. |
| `--yes`, `-y` | Skip confirmation. |

### `drone-cli template update`

Replace a template's YAML body.

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

**Arguments:** `name` (required)

| Option | Description |
| --- | --- |
| `--org`, `--namespace` | Namespace, e.g. acme. |
| `--from-file` | Read the new YAML body from this file. |
| `--from-stdin` | Read the new YAML body from stdin. |

## `user`

### `drone-cli user add`

Create a user. ADMIN ONLY (403 otherwise).

Two very different things behind one route:

  --machine   a bot account. Drone mints a token and RETURNS IT IN THIS ONE
              RESPONSE. There is no second chance: the hash is `json:"-"`, so
              no API call — not even as admin — can ever read it back. Lose it
              and your only recovery is delete + recreate. Capture it:
                  drone-cli user add ci-bot --machine --fields token

  (human)     Drone calls the SCM to resolve the login, and may overwrite the
              login/email you passed with what the SCM says. If the server's
              SCM link is dead this fails as HTTP 500 "Unauthorized" — that is
              the server's credentials, not yours (`drone-cli server doctor`).

`active` cannot be set: the server hardcodes it true. HTTP 402 means the
license seat limit is reached, not a bad request.

**Arguments:** `login` (required)

| Option | Description |
| --- | --- |
| `--machine` | Create a bot account and MINT ITS TOKEN (shown once, never again). |
| `--admin` | Grant system admin — bypasses every repo permission check. |

### `drone-cli user feed`

Your build feed: every repo you can see, with its LATEST build. Not admin-only.

Despite the endpoint being called `/api/user/builds`, it does not return
builds. It returns **repo objects**, one per repo, each carrying only its most
recent build — verified live. So this is a fleet-health snapshot, not a
history: `last_build_number` 42 tells you nothing about build 41.

Why it matters: **Drone has no cross-repo build search.** There is no
"show me every failing build" endpoint anywhere in the API; every other build
route is scoped to one repo. This single call is the only way to see the state
of everything at once — to find the red repos, fan out from here:

    drone-cli user feed --fields slug,last_build_status
    drone-cli build ls --repo <the red one>

Close cousin: `drone-cli repo ls --latest` returns the same shape from
`/api/user/repos?latest=true`. That one lists repos and can include ones that
have never built; this one is the activity feed. Same field names either way.

### `drone-cli user info`

Show one user. ADMIN ONLY (403 otherwise).

Users are addressed by login everywhere in this API; the numeric `id` in the
response is not usable as a path segment.

**Arguments:** `login` (required)

### `drone-cli user ls`

List every Drone user. ADMIN ONLY (403 otherwise).

The handler ignores every query parameter — no pagination, no filter, no sort,
no search — so it returns one full array and the filters above are applied
here, client-side. That is not a limitation to work around; it is the whole
API.

| Option | Description |
| --- | --- |
| `--admin` | Only system admins. |
| `--machine` | Only machine (bot) accounts. |
| `--inactive` | Only blocked accounts (active=false). |

### `drone-cli user rm`

Delete a user. ADMIN ONLY (403 otherwise). Wider blast radius than it looks.

Deleting a user does not only remove a login: the server asynchronously
transfers that user's repositories to another owner and fires a webhook. Any
token they held (a machine account's especially) stops working immediately and
is unrecoverable — there is no undo and no export.

**Arguments:** `login` (required)

| Option | Description |
| --- | --- |
| `--yes`, `-y` | Skip confirmation. |

### `drone-cli user update`

Change a user's flags. ADMIN ONLY (403 otherwise).

    drone-cli user update alice --admin          # promote
    drone-cli user update ci-bot --no-active     # block a bot, keep its repos

Every flag is TRI-STATE: pass `--admin` or `--no-admin` to set it, or leave it
off entirely to leave it alone. An omitted flag is omitted from the PATCH
body, never sent as false — this route patches only the keys it receives, so
a body that mentions a field you did not is a privilege change you did not
intend. At least one flag is required; an empty body would 200 having done
nothing, which reads as success.

Things that bite:
  * `--admin` is a real privilege boundary, not a label. A system admin reads
    and writes EVERY repo (and every repo's secrets) regardless of who owns
    them, and can mint machine tokens. It is not repo admin.
  * `--no-active` is the block switch: their token stops authenticating
    immediately. Their repos stay put (unlike `user rm`, which transfers them).
  * Flipping `--machine` does not create a token, and a machine account's
    token can never be read back — if you need one, `user add --machine`.
  * Revoking your own `--admin` (or `--active`) prompts first: this command is
    admin-only, so you cannot undo it yourself.
  * You cannot rename a user here; login is the address, not a field.

**Arguments:** `login` (required)

| Option | Description |
| --- | --- |
| `--admin`, `--no-admin` | Grant/revoke SYSTEM admin. PRIVILEGE CHANGE: an admin bypasses every repo permission check on every repo. Unset = leave as-is. |
| `--active`, `--no-active` | Unblock/block the account. --no-active kills their token everywhere. Unset = leave as-is. |
| `--machine`, `--no-machine` | Mark as a bot account. Does NOT mint a token — only `user add --machine` does. Unset = leave as-is. |
| `--yes`, `-y` | Skip the self-lockout confirmation. |

## `wait`

### `drone-cli wait`

Wait for a commit's build to finish. `--commit HEAD` after a push.

**Arguments:** `number` (optional)

| Option | Description |
| --- | --- |
| `--repo`, `-r` | owner/name. |
| `--commit`, `-c` | Wait for THIS commit's build. `HEAD` reads the local checkout. |
| `--event` | With --commit: which build to wait for. Default: push. |
| `--timeout` | Give up after this long, e.g. 90s, 30m, 2h. |
| `--appear-timeout` | How long to wait for the build to EXIST at all. |
| `--interval` | Seconds between polls. |
| `--exit-code` | Exit non-zero when the build did not succeed (20-29). |
| `--links` | Include derived commit/repo web URLs. |

