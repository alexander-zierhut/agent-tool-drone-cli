# drone-cli — the agent-ready Drone CI CLI

> A fast, scriptable **Drone CI CLI** for builds, pipeline logs, repositories,
> promotions and deployments — and the first Drone command-line tool designed to
> be driven by **AI agents** (Claude, Cursor, LLM tool-loops) as well as humans.

[![PyPI](https://img.shields.io/pypi/v/agent-tool-drone-cli)](https://pypi.org/project/agent-tool-drone-cli/)
[![CI](https://github.com/alexander-zierhut/agent-tool-drone-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/alexander-zierhut/agent-tool-drone-cli/actions/workflows/ci.yml)
![Python](https://img.shields.io/pypi/pyversions/agent-tool-drone-cli)
![License: MIT](https://img.shields.io/badge/license-MIT-green)
![Agent ready](https://img.shields.io/badge/agent-ready-8A2BE2)

**Install:** `pipx install agent-tool-drone-cli` — then run `drone-cli guide`.

[**agent-tool-drone-cli**](https://pypi.org/project/agent-tool-drone-cli/) (the
installed command is `drone-cli`) is a Python command-line interface for the
[Drone CI](https://www.drone.io/) REST API. It covers the day-to-day pipeline
workflow — **waiting on a commit's build**, **reading just the failing step's
logs**, listing and triggering **builds**, **restart**/**cancel**/**approve**,
**promotions and rollbacks**, repositories, and "what is actually live on prod" —
all with first-class JSON output so it slots straight into automation and AI
agents.

### Why this Drone CLI?

- 🎯 **Address builds by COMMIT, not by build number.** `drone-cli wait --commit HEAD`
  answers *"did the thing I just pushed pass?"* Build numbers are racy — if a
  colleague pushes while you do, "the latest build" is a coin flip and you'd
  report their failure as yours. You already know your SHA. `--commit HEAD` reads
  it from the local checkout. Nothing else in the ecosystem works this way.
- 🔬 **`drone-cli log failed` — the failing step, and nothing else.** Doing this by
  hand means: GET the build, walk `stages[]`, find the failed step, know that
  stage/step are **1-based ordinals** (not names, not ids), GET
  `logs/{stage}/{step}`, then strip the `{pos,out,time}` envelope. One command
  instead, `--tail`-bounded — so an agent spends its context on the fix rather
  than on `apt-get` noise.
- 🤖 **Agent-ready** — structured JSON on stdout, structured errors on stderr,
  stable exit codes, and a built-in **`drone-cli guide`** playbook so an agent can
  learn the tool from the tool itself. See [AGENTS.md](AGENTS.md).
- 🚀 **Deployments as first-class questions** — `drone-cli promote --commit HEAD`,
  `drone-cli deploy status --to prod` (*which commit is on prod right now?*),
  `drone-cli deploy of --commit HEAD` (*has my commit shipped?*). The API exposes
  the data; nothing surfaces the answer.
- ⏱️ **Durations that don't exist upstream** — Drone's API has **no duration field
  anywhere**, only raw epochs. This CLI derives build time and queue latency for
  you (and never mistakes `finished: 0` for 1970).
- 🖇️ **Four output formats** — `json` (default), `table`, `markdown`, `csv`; pick
  per command with `-o`, trim to exact `--fields`, or `--stream` NDJSON.
- 🧪 **Safe by default** — `--dry-run` previews any write at the transport layer,
  the client retries genuinely transient failures (and pointedly **not** Drone's
  500s), and `drone-cli context` gives sticky per-session defaults.
- 🔐 **Safe credentials** — API token in the OS keyring (Secret Service / macOS
  Keychain / Windows Credential Locker), with a `0600` file fallback.
  `DRONE_SERVER`/`DRONE_TOKEN` work for CI.
- 🧰 **Escape hatch** — `drone-cli raw` calls any endpoint the typed commands don't
  wrap. Drone's API is thinly and sometimes wrongly documented; this is how you
  check.

**Docs:** [Full command reference](docs/COMMANDS.md) · [Agent guide](AGENTS.md) · built-in: `drone-cli guide`

**Keywords:** Drone CI CLI, Drone command line, Drone API client, drone.io,
pipeline automation, CI/CD CLI, build logs, continuous integration tooling,
promote deployment, AI agent tool, LLM tooling, Claude, DevOps automation.

## The command surface

Everything is discoverable from the binary — `drone-cli --help`, then
`drone-cli guide` for the playbook and `drone-cli <group> --help` for any group.
The top level:

```text
 Usage: drone-cli [OPTIONS] COMMAND [ARGS]...

 Agent-friendly CLI for Drone CI (builds, logs, repos, secrets, crons,
 deployments).

 Output is JSON on stdout by default (errors are JSON on stderr with a non-zero
 exit code); add `-o table` or trim with `--fields number,status`. Address builds
 by COMMIT, not by number: `drone-cli wait --commit HEAD`.

 New here / no context? Run `drone-cli guide` for the full playbook.

╭─ Commands ───────────────────────────────────────────────────────────────────────╮
│ guide      Built-in operating guide — how to use this CLI without external docs. │
│ wait       Wait for a commit's build to finish. `--commit HEAD` after a push.    │
│ promote    Promote a commit/build to a target (default: prod).                   │
│ auth       Log in, log out, inspect credentials.                                 │
│ repo       Repositories: list, enable, sync, inspect.                            │
│ build      Builds: list, inspect, run, restart, cancel, wait.                    │
│ log        Build logs — including just the failing step.                         │
│ deploy     Deployments: what is on prod, and what got it there.                  │
│ secret     Repository secrets (values are write-only).                           │
│ orgsecret  Organisation secrets, shared across a namespace's repos.              │
│ cron       Scheduled builds — with a guard for Drone's seconds-first cron.       │
│ template   Pipeline templates (namespaced).                                      │
│ server     Server version, health, queue — and `server doctor`.                  │
│ user       Your build feed, and user administration (admin only).                │
│ raw        Escape hatch: call any API endpoint directly.                         │
│ settings   View & change CLI settings.                                           │
│ context    Sticky session defaults (repo, etc.) reused across commands.          │
│ install    Integrate with other tools (e.g. `install claude`).                   │
╰──────────────────────────────────────────────────────────────────────────────────╯
```

Global options — `-o/--output json|table|markdown|csv`, `--fields`, `--dry-run`,
`--stream`, `--no-context` — are stripped from argv before parsing, so they work
anywhere on the line. Full reference: [docs/COMMANDS.md](docs/COMMANDS.md).

## The name: `drone-cli`, not `drone`

The installed command is **`drone-cli`**. The name **`drone` belongs to the
official Go client** that most Drone users already have on their PATH, and this
project **deliberately does not claim it** — the two are designed to coexist on
the same machine, and clobbering the binary everyone's muscle memory and existing
scripts point at would be a hostile way to install software.

`drone-agent` is refused for the same reason: in Drone's own vocabulary an
"agent" is the *runner*. Alias it yourself if you want it shorter:
`alias dc=drone-cli`.

## Compatibility

- **Drone:** 2.x — the API is unversioned and mounted at `<server>/api`. Verified
  live against `drone/drone:2` (2.28.x) with a Docker runner; Drone is in
  maintenance, so there is no version matrix to chase.
- **SCM:** Gitea / Forgejo verified live; GitHub / GitLab / Bitbucket share the
  same Drone API. The one SCM-shaped setting is the commit-URL pattern
  (`drone-cli settings set-scm`) — see the limitations below for why it can't be
  auto-detected.
- **Python:** 3.10+.

Anything not wrapped is reachable with `drone-cli raw <method> <path>`.

---

## Quick start

### 1. Install

**a) `pipx` (recommended — isolated, puts `drone-cli` on your PATH)**
```bash
pipx install agent-tool-drone-cli
```

**b) `pip`**
```bash
pip install agent-tool-drone-cli
# or, from a clone:
python3 -m venv .venv && . .venv/bin/activate && pip install -e .
```

**c) Single self-contained binary (no Python on the target)**

Download the prebuilt binary for your OS from the GitHub Releases page:
```bash
chmod +x drone-cli-linux-x86_64 && mv drone-cli-linux-x86_64 /usr/local/bin/drone-cli
drone-cli --help
```
Or build one yourself — a single file that bundles the interpreter, the deps and
the OS keyring backends:
```bash
pip install -e '.[build]'
python scripts/build_binary.py            # -> dist/drone-cli   (.exe on Windows)
```
`.github/workflows/release.yml` builds Linux/macOS/Windows binaries on every `v*`
tag and attaches them to the release.

### 2. Log in

```bash
drone-cli auth login                      # asks for the server, then shows you
                                          # exactly where to get a token
drone-cli auth whoami
```

`login` verifies the token before persisting anything, saves the connection
profile to `~/.config/drone-cli/config.json`, and stores the token in your
keyring. Get a token at `<your-drone-server>/account`.

For headless/CI use, skip the keyring entirely — the ecosystem's own env vars are
read as-is:

```bash
export DRONE_SERVER=https://drone.example.com
export DRONE_TOKEN=xxxxxxxx
drone-cli build ls --repo octocat/hello-world
```

> **Precedence hazard, stated plainly:** env beats keyring. That is what makes
> the tool work in CI — but `DRONE_*` is *also* the namespace the Drone runner
> injects into every build step, so an exported `DRONE_TOKEN` silently overrides
> your keyring login. `drone-cli auth status` always names the token actually in
> use. Don't guess.

### Use with Claude Code

```bash
drone-cli install claude          # writes ~/.claude/skills/drone-ci/SKILL.md
drone-cli install claude --print  # preview it first
```

The skill points Claude at `drone-cli guide`, so it learns the tool from the
tool. On the first interactive run, if Claude Code is detected, the CLI offers to
install it — once. Decline and nothing changes.

---

## Command highlights

Run `drone-cli <group> --help` for full options, or `drone-cli guide <topic>`.

### The whole point — `wait`

```bash
drone-cli wait --commit HEAD                       # did my push pass?
drone-cli wait --commit HEAD --timeout 20m
drone-cli wait --commit HEAD --exit-code && ./deploy.sh
```

Three outcomes an agent must distinguish, and this command keeps them apart:
**finished** (the outcome is in the JSON), **blocked** (a human must approve — the
approve command is included in the output), and **never appeared** (exit 10: the
webhook may be broken or the repo not enabled — that is *not* "the tests failed").

### Logs — `drone-cli log`

```bash
drone-cli log failed --commit HEAD --tail 80   # just the failing step
drone-cli log failed 42 --all                  # every failing step, not just the first
drone-cli log view 42 --stage 1 --step 2       # 1-based ordinals, not names
drone-cli log view 42 --grep "error"
```

### Builds — `drone-cli build`

```bash
drone-cli build ls --repo octocat/hello-world --status failure
drone-cli build info --commit HEAD --links      # stages[].steps[] + a real commit URL
drone-cli build run --branch main               # NOTE: event = "custom", not "push"
drone-cli build restart 42                      # -> creates a NEW build number
drone-cli build cancel 42
drone-cli build approve 42 --stage 2            # blocked builds
```

### Deployments — `drone-cli promote` / `drone-cli deploy`

```bash
drone-cli promote --commit HEAD                 # --to defaults to `prod`
drone-cli promote --commit HEAD --to staging
drone-cli deploy status --to prod               # which commit is live on prod?
drone-cli deploy of --commit HEAD               # has my commit been promoted?
drone-cli settings set-promote-target staging   # change the default target
```

### Output, context, escape hatch

```bash
drone-cli build ls -o table
drone-cli build ls --fields number,status,after     # trim the payload
drone-cli build ls --stream                          # NDJSON
drone-cli build restart 42 --dry-run                 # prints the request, sends nothing

drone-cli context set --repo octocat/hello-world     # stop repeating --repo
drone-cli context show                               # incl. where each value came from
drone-cli --no-context build ls                      # ignore it for one command

drone-cli raw get repos/octocat/hello-world/builds
```

---

## Output for agents

- Default output is JSON on **stdout**. Errors are JSON on **stderr** with a
  non-zero exit code (`{"error": "...", "status": 404}`).
- **Exception:** `log view` / `log failed` print raw log **text**. Logs are prose,
  not records — don't `json.loads` them.
- Exit codes: `0` ok · `1` generic · `3` config · `4` auth · `5` not-found ·
  `7` validation · `8` not-implemented-on-this-server · `9` wait timed out ·
  `10` no build for that commit · `130` interrupted. (`6` is reserved
  family-wide for conflicts; Drone has no optimistic locking and never conflicts.)
- `wait --exit-code` opts into a separate **20–29** band so "the build failed"
  can never be confused with "the CLI failed".

Full contract: [AGENTS.md](AGENTS.md).

---

## Contributing / Development

**You do not need Drone, Docker, a server, or a token to contribute.** Clone it
and run:

```bash
pip install -e '.[test]'
pytest                    # green on a clean checkout, ~2s, no server needed
```

That is the whole setup. The suite is green on a fresh clone with nothing
running: the tests that need a live instance detect there isn't one and skip with
a message telling you exactly what to set. `make test-unit` runs the same
hermetic set explicitly (`pytest -m "not integration"` — the marker is the source
of truth, never a file list).

Please add a test with your change; the hermetic suite is fast enough to run on
every save.

### The deeper tier (only if you're touching the client↔server seam)

A live Drone needs an SCM behind it (repos only enter Drone by syncing from
Gitea/Forgejo/GitHub), a runner to execute anything, and a token injected into
the DB. `spike/docker-compose.spike.yml` and `spike/VERIFIED_FINDINGS.md` document
a stack that was proven end-to-end. Point the suite at any live server with:

```bash
export DRONE_SERVER=http://localhost:8080
export DRONE_TOKEN=<your token>
pytest                    # the integration tests light up automatically
```

Unset those and they skip again. `spike/VERIFIED_FINDINGS.md` is the authoritative
record of what this API actually does — it is worth more than the official docs,
which are wrong in several documented places.

---

## Security notes

- The API token is the only secret persisted, and it goes to the OS keyring by
  default. `drone-cli auth status` shows which backend is in use.
- On a headless box with no Secret Service, the token falls back to a `0600` file
  under `~/.config/drone-cli/` and the CLI says so.
- `DRONE_TOKEN` / `DRONECLI_TOKEN` in the environment always win (nothing is
  written to disk in that mode).
- Tracebacks never show locals — they hold the token, and a pretty traceback
  would print it into your CI log.

## Known limitations (Drone's API, not the CLI)

These are properties of Drone itself, verified live during a spike against a real
server. The CLI works around what it can and is loud about what it can't.

- **There is no duration field. Anywhere.** Not on builds, not on stages, not on
  steps — only raw `created`/`started`/`finished` epochs. Every duration you see
  here is derived client-side. (Also why `finished: 0` means *still running*, not
  1970.)
- **Secret values can never be read back.** Every read *and* write handler returns
  the secret without its data, by design — `GET .../secrets/{name}` yields
  `{id, repo_id, name}`. There is no `--show-value` that could ever exist, and no
  export, copy-between-repos, or value drift-check. That a read-back returns no
  value is not a failure.
- **No cross-repo build search.** No endpoint filters builds by status, event,
  author or date across repos; even within a repo only `branch`/`tag` are
  server-side. Anything else is a client-side scan under a page budget — which is
  why a search can honestly report "I stopped looking", never "nothing found".
- **Cron is seconds-first.** Drone uses a 6-field parser
  (`Second Minute Hour Dom Month Dow`). A normal 5-field crontab `"0 3 * * *"`
  **parses fine** and means *every hour at :03* — not 03:00 daily. Silent, 24×
  wrong, no error. The API also can't preview a schedule: `next` is only computed
  after the cron is persisted.
- **`build.link` is not a commit link.** For a `push` it's a **compare** URL
  (`/compare/before...after`); for an API-triggered build it's an **API** URL that
  renders as JSON. Drone passes through whatever the SCM handed it and never
  normalises. `--links` derives a real one from `{repo.link}/commit/{sha}` —
  and since **`repo.scm` is an empty string even on a synced repo**, the provider
  is not discoverable, so the pattern is a setting (`settings set-scm`) rather
  than a guess.
- **A 500 saying `Unauthorized` is not about your token.** It means the *server's*
  link to the git provider is broken. The CLI maps it to a real auth error that
  says so, and never retries it.
- **Some endpoints in the docs don't exist**: `POST .../lint` and `POST .../verify`
  are 404s, and `GET /api/version` is a 404 (the real one is `GET /version` on the
  web root). Others are documented with the wrong ACL. See
  `spike/VERIFIED_FINDINGS.md`.

## Part of the family

Built on **[agent-tool-shared-cli](https://github.com/alexander-zierhut/agent-tool-shared-cli)** —
the chassis every tool in this family shares: JSON on stdout, JSON errors on
stderr, a stable cross-tool exit-code contract, `--dry-run`, four output formats,
and a built-in `guide` so an agent can learn each tool from the tool itself.

| Tool | Install | For |
| --- | --- | --- |
| [**drone-cli**](https://github.com/alexander-zierhut/agent-tool-drone-cli) | `pipx install agent-tool-drone-cli` | Drone CI — builds, failing-step logs, promotions |
| [**grafana-cli**](https://github.com/alexander-zierhut/agent-tool-grafana-cli) | `pipx install agent-tool-grafana-cli` | Grafana — log discovery, health scan, alert routing |
| [**openproject**](https://github.com/alexander-zierhut/agent-tool-openproject-cli) | `pipx install agent-tool-openproject-cli` | OpenProject — work packages, time, invoicing |
| [**lexware-office**](https://github.com/alexander-zierhut/agent-tool-lexware-office-cli) | `pipx install agent-tool-lexware-office-cli` | Lexware Office — invoices, contacts, AR-aging |

They compose over the shared contract:
`drone-cli wait --commit HEAD && grafana-cli scan --since 10m` answers *"my commit
built — is it healthy in production?"*

## License

MIT — see [LICENSE](LICENSE).
