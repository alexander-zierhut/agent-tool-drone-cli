"""`drone-cli guide` — the built-in operating manual.

The point is self-sufficiency: an agent with only this binary and no other
context can run `drone-cli guide` and learn the output contract, how to
authenticate, the domain model, and the gotchas — with no README, no network and
no config. It must therefore be structurally impossible for this command to fail
with a config or auth error, or to block on a prompt.

Every gotcha below was VERIFIED against a live Drone 2.28 during a spike. They
are not folklore; they are the things that will otherwise cost an agent a
wrong answer.
"""

from __future__ import annotations

import typer

OVERVIEW = """\
drone-cli — operating guide (run `drone-cli guide <topic>` for details)

WHAT IT IS
  A CLI for Drone CI: repos, builds, logs, secrets, crons, promotions and
  deployments — designed to be driven by an AI agent.

  This is NOT the official `drone` binary. Both can be installed side by side:
  `drone` is the official Go client; `drone-cli` is this tool.

OUTPUT CONTRACT (important for scripting/agents)
  - stdout is JSON by default — parse it.
  - Errors go to stderr as JSON with a non-zero exit code: {"error": "...", "status": 404}.
  - EXCEPTION: `log view/failed` print raw log TEXT, not JSON. Logs are prose.
  - Exit codes: 0 ok · 1 generic · 3 config · 4 auth · 5 not-found · 7 validation
                8 not-implemented-on-this-server · 9 wait timed out
                10 no build for that commit · 130 interrupted
    (6 is reserved family-wide for conflicts; Drone never conflicts.)
  - Change format anywhere on the line: `-o table`, `-o markdown`, `-o csv`.
  - Trim output: `--fields number,status,after`. Big lists: `--stream` (NDJSON).
  - PREVIEW a write without doing it: add `--dry-run` — prints the request, exits 0.

AUTHENTICATE
  Interactive:      drone-cli auth login          (asks for the server, then shows
                                                   you exactly where to get a token)
  Non-interactive:  export DRONE_SERVER=https://drone.example.com
                    export DRONE_TOKEN=xxxxxxxx
  Get a token:      <your-drone-server>/account
  Check:            drone-cli auth status         (says WHICH backend/token is in use)

  NOTE: DRONE_TOKEN in the environment OVERRIDES a keyring login, silently. If
  results look wrong, run `auth status` — it names the token actually in use.

THE ONE THING TO KNOW: ADDRESS BUILDS BY COMMIT
  Build NUMBERS are racy. If two people push at once, "the latest build" is a
  coin flip and you may report someone else's failure as your own. You know your
  SHA; use it.

    drone-cli wait --commit HEAD              # after a push: did MY commit pass?
    drone-cli build info --commit <sha>
    drone-cli promote --commit HEAD --to prod

  `--commit HEAD` reads the local git checkout.
  Also: build NUMBER != build ID. Both fields exist in the JSON; every path uses
  the per-repo `number`. Stage/step are 1-based ORDINALS, not ids and not names.

KEY GOTCHAS (each verified live — save yourself a wrong answer)
  - `restart` creates a NEW build number. It does not resume the old one.
  - Secret VALUES can never be read back. The API omits them by design; a
    `secret get` returns only the name. That is not a failure.
  - Cron is SECONDS-FIRST: "0 3 * * *" fires EVERY HOUR at :03, not daily at 3am.
    Use `cron add --at "3am daily"` and let the CLI build the expression.
  - `build.link` is NOT a commit link (it is a compare URL for pushes, and an API
    URL for API-triggered builds). Use `--links`, which derives a real one.
  - A 500 saying "Unauthorized" means the SERVER's link to your git provider is
    broken — not your token. `drone-cli server doctor` explains it.
  - A repo must be enabled before it has builds: `drone-cli repo enable <slug>`.
  - Repos are addressed `owner/name` (a slug), never a numeric id.

DISCOVER
  drone-cli --help  ·  drone-cli <group> --help

USE WITH CLAUDE CODE
  `drone-cli install claude` registers a skill so Claude uses this automatically.

TOPICS:  builds · commits · deploy · logs · secrets · cron · repos · output · auth · context · settings · gotchas
"""

TOPICS: dict[str, str] = {
    "commits": """\
COMMITS — the primary way to address work

  Build numbers are racy; a commit SHA is not. Everything accepts --commit.

    drone-cli wait --commit HEAD                  # did my push pass?
    drone-cli wait --commit HEAD --timeout 20m
    drone-cli build info --commit <sha>
    drone-cli build ls --commit <sha>             # ALL builds for it (push, restarts, promotes)

  `--commit HEAD` resolves from the local git checkout. Short SHAs work.

  ONE COMMIT, MANY BUILDS. A push builds it; a restart makes ANOTHER build; each
  promote makes another still. So say which you mean:
    --event push        (the default for `wait`: "did my push pass?")
    --event promote --to prod

  "NO BUILD YET" IS NORMAL. After a push there is real webhook latency before a
  build exists. `wait` treats that as "still waiting" for a grace period, then
  exits 10 (commit-not-built) — distinct from a failure, so an agent can tell
  "the hook is broken" from "the tests are red".

  DEPLOYMENT QUESTIONS:
    drone-cli deploy status --to prod            # which commit is on prod RIGHT NOW
    drone-cli deploy of --commit HEAD            # has my commit been promoted?
""",
    "builds": """\
BUILDS
  list:    drone-cli build ls [--repo o/n] [--branch main] [--event push] [--status failure]
  info:    drone-cli build info <number|--commit SHA>     # embeds stages[].steps[]
  run:     drone-cli build run --branch main              # event = "custom"
  restart: drone-cli build restart <number>               # -> a NEW build number
  cancel:  drone-cli build cancel <number>
  wait:    drone-cli wait --commit HEAD [--timeout 30m] [--exit-code]
  approve: drone-cli build approve <number> --stage 2     # for blocked builds

  STATUSES: pending, running, blocked, waiting_on_dependencies (all in flight);
            success, failure, killed, error, skipped, declined (terminal).
  EVENTS:   push, pull_request, tag, promote, rollback, cron, custom.

  `blocked` means a human must approve a stage. `wait` treats it as TERMINAL and
  tells you the approve command — otherwise it would hang until the timeout.

  --exit-code makes `wait` exit non-zero when the build failed (20-29 band), so
  `drone-cli wait --commit HEAD --exit-code && ./deploy.sh` works. WITHOUT it,
  exit 0 means "I observed the outcome"; the outcome is in the JSON.

  There is NO duration field in the Drone API. This CLI derives it.
""",
    "deploy": """\
DEPLOYMENTS — promote, and what is live

  promote: drone-cli promote --commit HEAD                # --to defaults to `prod`
           drone-cli promote --commit HEAD --to staging
           drone-cli promote <build-number> --to prod
           (change the default: `drone-cli settings set-promote-target staging`)

  what is live:
           drone-cli deploy status                        # which commit is on prod
           drone-cli deploy status --to staging
           drone-cli deploy ls --to prod                  # deployment history

  has my commit shipped?
           drone-cli deploy of --commit HEAD              # every promotion of it
           drone-cli deploy of --commit HEAD --to prod

  "What is on prod" = the newest SUCCESSFUL promote/rollback to that target. A
  FAILED promote deployed nothing and is never reported as live.

  A promote creates a NEW build (event=promote) from the same commit. Promoting
  does not re-run the push build.

  WARNING: promote/restart handle parameters differently — a restart inherits the
  PREVIOUS build's params and they beat anything you pass. The CLI warns you.
""",
    "logs": """\
LOGS — the payload, and the token sink

  drone-cli log failed <number|--commit SHA>      # ONLY the failing step's tail
  drone-cli log view <number> --stage 1 --step 2  # one step, in full
  drone-cli log failed --commit HEAD --tail 80

  `log failed` is the one to reach for. Without it, finding out why a build broke
  costs: GET the build -> walk stages[] -> know stage/step are 1-based NUMBERS
  (not names) -> GET that step's logs -> strip the {pos,out,time} envelope. Four
  calls and three pieces of tribal knowledge, and the payload is unbounded prose
  that will eat your context on apt-get noise.

  LOGS ARE TEXT, NOT JSON. This is the one place stdout is not machine-readable.
  Use --raw for the {pos,out,time} envelope if you really want it.

  Logs for a step that never ran give 404 — that is "it never ran", not an error.
  Finished builds keep logs until purged; a purge is irreversible.
""",
    "secrets": """\
SECRETS
  repo:  drone-cli secret ls --repo o/n
         drone-cli secret set NAME --value V --repo o/n      # idempotent upsert
         drone-cli secret rm NAME --repo o/n
  org:   drone-cli orgsecret ls --org acme        (path is /api/secrets/{namespace})

  VALUES ARE UNREADABLE, BY DESIGN. Every handler strips `data` before responding
  (verified live: a GET returns only {id, repo_id, name}). There is no
  --show-value and there never can be. An agent that reads back to verify a write
  gets only the name — that is success, not failure.

  `--dry-run` on a secret write REDACTS the value it prints.
  `pull_request: true` exposes the secret to PRs from forks. Think first.
""",
    "cron": """\
CRON
  drone-cli cron ls --repo o/n
  drone-cli cron add nightly --at "3am daily" --repo o/n
  drone-cli cron exec nightly --repo o/n           # run it now -> a build (event=cron)

  *** DRONE'S CRON IS SECONDS-FIRST ***
  It uses a 6-field parser: SECOND minute hour dom month dow.
  So the standard crontab "0 3 * * *" parses FINE and means second=0, minute=3,
  hour=* -> it fires EVERY HOUR at :03. Silently. 24x more often than you meant.

  This CLI detects a 5-field expression, refuses it, and prints the next 5 fire
  times before creating anything. Prefer --at / --every and never hand-write it.

  `cron update --expr` is silently DROPPED by the server (200, unchanged). The
  CLI emulates it and verifies.
""",
    "repos": """\
REPOS
  drone-cli repo ls [--all]
  drone-cli repo sync                     # pull the list from your git provider
  drone-cli repo enable owner/name        # activate CI (creates the webhook)
  drone-cli repo info owner/name

  Repos are addressed `owner/name` — a slug, never a numeric id.

  Drone does NOT own repos: they mirror from the git provider (Forgejo/Gitea/
  GitHub). A brand-new repo is invisible until `repo sync`. A repo has no builds
  until `repo enable`.

  `enable` silently sets repo.UserID = you, moving every webhook, clone and
  config fetch onto YOUR git token. The CLI warns.
""",
    "output": """\
OUTPUT & FIELDS
  Formats: json (default), table, markdown, csv.
     drone-cli build ls -o table
     drone-cli build ls -o csv > builds.csv
  Default:  drone-cli settings set-format table
  Fields:   drone-cli build ls --fields number,status,after,author_login
            (dotted paths work: --fields number,stages.0.name)
  Streaming: --stream emits NDJSON, one object per line.

  -o/--format/-f and --fields work ANYWHERE on the line, before or after the
  subcommand. Precedence: --format > $DRONECLI_FORMAT > saved default > json.

  Commit links: add --links to get commit_url/repo_url. Drone's own `build.link`
  is NOT a commit link — for a push it is a compare URL, for an API build an API
  URL. If your provider isn't Gitea/Forgejo/GitHub:
  `drone-cli settings set-scm gitlab`.
""",
    "auth": """\
AUTH & PROFILES
  login:   drone-cli auth login                      (interactive; shows the token URL)
           drone-cli auth login --server https://drone.example.com --token xxx
  check:   drone-cli auth status   |   drone-cli auth whoami
  logout:  drone-cli auth logout

  Env (non-interactive, no keyring — for CI):
     DRONE_SERVER / DRONE_TOKEN        the ecosystem standard (official CLI's names)
     DRONECLI_SERVER / DRONECLI_TOKEN  ours; these win if both are set

  PRECEDENCE: env > keyring > file. So an exported DRONE_TOKEN OVERRIDES your
  keyring login, silently. That is deliberate (CI depends on it) — but if
  something looks wrong, `auth status` names the exact token in use.

  Multiple servers: `drone-cli -p prod build ls`.
  Get a token: <server>/account
""",
    "context": """\
SESSION CONTEXT — sticky defaults so you stop repeating --repo
  A CLI has no live process, so "context" = durable saved defaults.

  set:    drone-cli context set --repo octocat/hello-world
  show:   drone-cli context show          # ALWAYS check this if scoping looks wrong
  clear:  drone-cli context clear   |   unset one: drone-cli context unset repo
  save:   drone-cli context save myproj   |   use: drone-cli context use myproj

  How it applies: for any command with a matching option (--repo, --owner,
  --branch), the context fills it IF you did not pass the flag. Explicit flags
  always win. `--no-context` ignores it for one command.

  AGENT NOTE: this is IMPLICIT state that changes results. If output looks
  wrongly scoped, run `context show` or add `--no-context`.
""",
    "settings": """\
SETTINGS — each has a sane default; the CLI asks once on first run where it can't
  drone-cli settings show
  drone-cli settings set-format table          # json (default) | table | markdown | csv
  drone-cli settings set-promote-target prod   # what `promote` uses without --to
  drone-cli settings set-scm gitea             # commit-link flavour:
                                               #   gitea/forgejo/github -> /commit/{sha}
                                               #   gitlab               -> /-/commit/{sha}
                                               #   bitbucket            -> /commits/{sha}
  drone-cli settings set-scm-base-url https://git.example.com/o/n
                                               # only if repo.link is wrong/unreachable

  Why set-scm exists: Drone's repo object has an `scm` field and it is an EMPTY
  STRING even on a fully synced repo (verified live) — the provider is simply not
  discoverable from the API, so we default to the Gitea/Forgejo/GitHub shape
  rather than guess.
""",
    "gotchas": """\
GOTCHAS — every one of these was VERIFIED against a live Drone; each is a wrong
answer waiting to happen. Read this before you trust an unexpected result.

  ADDRESSING
  - Build NUMBER != build ID. Both fields are in the JSON; every API path and
    every command here takes the per-repo `number`. The `id` is global and using
    it silently addresses a DIFFERENT build (or 404s).
  - Stage and step are 1-based ORDINALS — not ids, and not names. `--stage 1
    --step 2` means "the second step of the first stage".
  - Repos are addressed `owner/name` (a slug), never a numeric id.
  - Build numbers are RACY. Two people pushing at once means "the latest build"
    may be someone else's, and you would report their failure as yours. Address
    builds by commit: `drone-cli wait --commit HEAD`.

  BUILDS
  - `build restart` mints a NEW build number — it does not resume or reuse the
    old one. (Restarting #1 produces #2.) The PREVIOUS build's parameters also
    take precedence over any you pass.
  - API-triggered builds have event=`custom`, NOT `push`. So `build run` against
    a pipeline gated on `trigger: event: [push]` will skip — that is the
    pipeline's config, not a CLI bug. It surprises everyone exactly once.
  - A repo must be ENABLED before it has builds at all: `drone-cli repo enable
    <slug>`. Before that, its build endpoints 404. A brand-new repo is not even
    visible until `drone-cli repo sync`.
  - `blocked` means a human must approve a stage. `wait` treats it as terminal
    and prints the approve command, rather than hanging until the timeout.

  SECRETS
  - Secret VALUES can NEVER be read back. Every handler strips `data` before
    responding (a GET returns only {id, repo_id, name}), so there is no
    --show-value and there never can be. An agent that reads a secret back to
    verify a write gets only the name — that is SUCCESS, not failure.

  CRON
  - Drone's cron is SECONDS-FIRST: a 6-field parser, SECOND minute hour dom
    month dow. The standard crontab "0 3 * * *" therefore parses FINE and means
    second=0, minute=3, hour=* — it fires EVERY HOUR at :03, silently, 24x more
    often than you meant. Use `cron add --at "3am daily"` and let the CLI build
    the expression; it refuses 5-field input and prints the next fire times.

  LINKS
  - `build.link` is NOT a commit link. Drone passes through whatever the SCM
    gave it and never normalizes: for a push it is a COMPARE url
    (/compare/<before>...<after>), for an API-triggered build it is an API url.
    Use `--links`, which derives a real commit url from repo.link.

  ERRORS THAT LIE
  - A 500 whose body says "Unauthorized" means the SERVER's link to your git
    provider is broken — NOT your token. Drone maps SCM auth failures to 500.
    Chasing your own credentials there wastes the whole debugging session; run
    `drone-cli server doctor`, which explains it.
  - `GET /api/version` DOES NOT EXIST (404), despite what the docs imply. The
    real, unauthenticated endpoint is `GET /version`. `drone-cli server version`
    already knows this.

  OUTPUT
  - `log view` / `log failed` print raw log TEXT, not JSON — the one place
    stdout is not machine-readable. Do not parse it as JSON.
  - A red build is exit 0: the CLI ran fine and the outcome is in the JSON. Only
    `wait --exit-code` opts into the 20-29 band.
""",
}


def guide(
    topic: str = typer.Argument(
        None, help="Optional topic: commits, builds, deploy, logs, secrets, cron, repos, output, auth, context, settings, gotchas."
    ),
) -> None:
    """Print a built-in operating guide so the CLI is usable without external docs.

    Run `drone-cli guide` for the overview, or `drone-cli guide <topic>` for a
    focused cheat-sheet (e.g. `drone-cli guide commits`).
    """
    if not topic:
        typer.echo(OVERVIEW)
        return
    key = topic.strip().lower()
    text = TOPICS.get(key)
    if text is None:
        typer.echo(f"unknown topic '{topic}'. Available: " + ", ".join(TOPICS) + "\n")
        typer.echo(OVERVIEW)
        raise typer.Exit(code=2)
    typer.echo(text)
