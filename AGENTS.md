# Using `drone-cli` from an AI agent

The **machine contract** for driving this CLI. For the human tutorial see the
[README](README.md); for every option see [docs/COMMANDS.md](docs/COMMANDS.md).

> **No context? Start here:** run **`drone-cli guide`**. The CLI ships its own
> playbook (output contract, auth, the domain model, verified gotchas) so you can
> bootstrap from the binary alone, with no external docs and no network. Then
> `drone-cli guide <topic>` — `commits`, `builds`, `deploy`, `logs`, `output`, …

> This is **not** the official `drone` Go client. Both coexist; ours is `drone-cli`.

## The output contract

- **stdout is JSON.** Parse it. Success exits `0`.
- **Errors are JSON on stderr**, non-zero exit: `{"error": "...", "status": 404}`.
- **The one exception: `log view` / `log failed` print raw log TEXT**, not JSON.
  Logs are prose. `json.loads` on them will crash.
- **Exit codes** are stable, published API — branch on them:

  | Code | Meaning |
  | --- | --- |
  | 0 | success (including a successful `--dry-run`) |
  | 1 | generic error |
  | 3 | config (no profile / no repo / bad config) |
  | 4 | auth (401/403 — token missing or wrong) |
  | 5 | not found (404) |
  | 7 | validation (400 — Drone has no 422) |
  | 8 | not implemented on this server (501) |
  | 9 | wait timed out — the build did **not** finish, and was **not** observed to fail |
  | 10 | no build exists for that commit |
  | 130 | interrupted (SIGINT) |

  `2` is Click/Typer usage error — never allocated. **`6` is reserved family-wide
  for conflicts and is unused here**: Drone has no optimistic locking, so nothing
  409s. It is not recycled — an agent that learned "6 = conflict" from a sibling
  CLI must never meet a different meaning here.

- **Build status never leaks into the exit code by default.** Observing a red
  build is a *successful* run of this tool: exit `0`, outcome in the JSON.
  `wait --exit-code` opts into a **20–29** band (20 failed, 24 blocked) that
  cannot collide with the error codes above. "The CLI failed" and "the thing the
  CLI watched failed" are different facts.

## Run it non-interactively

```bash
export DRONE_SERVER=https://drone.example.com
export DRONE_TOKEN=xxxxxxxx
drone-cli build ls --repo octocat/hello-world
```

`DRONECLI_SERVER`/`DRONECLI_TOKEN` are higher-precedence aliases. Env beats
keyring beats file — so an exported `DRONE_TOKEN` **silently overrides** a
keyring login, and `DRONE_*` is also what the Drone runner injects into every
build step. If results look wrong, `drone-cli auth status` names the token
actually in use.

The CLI never prompts unless **stdin and stdout are both TTYs**; `CI=true` and
`DRONE=true` also disable it. A pipeline can never be blocked by a question.

## The premise: address builds by COMMIT, not number

Build **numbers are racy**. Two people pushing at once means "the latest build"
may be someone else's, and waiting on it reports their failure as yours. The
commit SHA is the only stable handle you already have.

```bash
drone-cli wait --commit HEAD                  # did MY push pass?
drone-cli build info --commit <sha>
drone-cli promote --commit HEAD --to prod
```

`--commit HEAD` resolves from the local git checkout; short SHAs work. Three
distinct outcomes, and conflating them is the failure mode this design exists to
prevent:

| Outcome | Exit | Meaning |
| --- | --- | --- |
| finished | 0 | status is in the JSON (`status`, `succeeded`, `failed_steps`) |
| blocked | 0 | a human must approve; the exact `build approve` command is in `note` |
| never appeared | 10 | the webhook may be broken, or the repo not enabled. **Not** "the tests failed" |
| still running at deadline | 9 | nothing was observed. **Not** a failure |

**One commit can have many builds** (a push, a restart, several promotes), so say
which you mean: `--event push` (the default for `wait`), `--event promote`.
"No build yet" right after a push is **normal** — there is real webhook latency —
so `wait` treats it as waiting for a grace period before exiting 10.

Also: build **NUMBER** ≠ build **ID** (both are in the JSON; every path uses
`number`). Stage/step are **1-based ordinals**, not ids and not names. Repos are
`owner/name` slugs, never ids.

## Spend fewer tokens

- **`--fields`** trims the payload — dotted paths work:
  `drone-cli build ls --fields number,status,after`
- **`--limit N`** caps rows.
- **`--stream`** emits NDJSON, one object per line, as fetched.
- **`log failed --tail 80`** instead of a whole build log. A full pipeline log is
  unbounded prose; the failing step is what you needed.
- **`-o csv`** / `-o table` / `-o markdown` when JSON isn't the right shape.

Of those, only the **globals** work **anywhere on the line** — before or after
the subcommand: `--output`/`-o`, `--format`/`-f`, `--fields`/`--columns`,
`--dry-run`, `--stream` and `--no-context`. They're stripped from argv before
parsing, which is why **no command may declare one of those names** as its own
option — the popper would eat it first, silently.

`--limit` and `--tail` are ordinary per-command options: they must follow the
subcommand they belong to.

## Preview writes

Add **`--dry-run`** to any mutating command. It is intercepted in the transport,
so every write gets it and none can bypass it. Reads still execute (resolving a
commit to a build number must really happen, or the printed request would be a
guess).

```bash
drone-cli build restart 42 --dry-run
# -> {"dryRun": true, "request": {"method": "POST", "url": "...", "params": {...}}}
```

## Session context (sticky defaults) — and its caveat

`drone-cli context` stores durable defaults in config (a CLI has no live process,
so "context" = saved defaults, not a session). Mostly: stop repeating `--repo`.

```bash
drone-cli context set --repo octocat/hello-world
drone-cli context show          # each value + where it came from
drone-cli context clear
```

**This is implicit state that changes results.** Rules:

- **Explicit flags always win.** `--repo other/thing` overrides.
- **`--no-context`** ignores it for one command.
- Context only fills **options**, never positional arguments — deliberately, so a
  sticky repo can never silently supply the target of a destructive command.
- Don't assume a fresh environment is context-free. **Run `context show` at the
  start of a task**, and be explicit in scripts you don't control.

## Recipes

```bash
# The core loop: push, wait, and if it's red, read only what broke
git push && drone-cli wait --commit HEAD || drone-cli log failed --commit HEAD --tail 80

# Gate a deploy on the build (--exit-code opts into the 20-29 band)
drone-cli wait --commit HEAD --exit-code && ./deploy.sh

# Ship it, then confirm what is actually live
drone-cli promote --commit HEAD --to prod
drone-cli deploy status --to prod --fields after,author_login,finished

# Has this commit already shipped?
drone-cli deploy of --commit HEAD --to prod

# Triage a repo without paging a human
drone-cli build ls --repo octocat/hello-world --status failure --limit 5

# Anything not wrapped — the API is thinly documented; this is how you check
drone-cli raw get repos/octocat/hello-world/builds/42
```

## Gotchas worth knowing (all verified against a live Drone)

- **`build run` produces `event=custom`, never `push`.** A pipeline gated on
  `trigger: event: [push]` will silently not run. This is a pipeline-config issue,
  not a CLI bug, and it surprises everyone exactly once.
- **`restart` creates a NEW build number** — it does not resume the old one. The
  **previous** build's parameters also take precedence over any you pass.
- **`promote` creates a new build** (`event=promote`) from the same commit; it
  does not re-run the original.
- **Secret values can never be read back.** A read-back that returns no value is
  the API working as designed, not an error. Don't retry it, don't report it as
  a failure.
- **Cron is seconds-first.** `"0 3 * * *"` parses fine and fires **every hour at
  :03**, not daily at 03:00. Never hand-assemble a 6-field string.
- **`build.link` is not a commit link** (compare URL for pushes, API URL for
  API-triggered builds). Use `--links`, which derives a real one.
- **A 500 saying `Unauthorized` means the *server's* SCM link is dead**, not your
  token. The CLI says so and does not retry it.
- **There is no duration field in the API** — every duration here is derived.
- Drone has **no comments, notifications, custom fields, attachments, members or
  time logging**. It is a build system, not a tracker. If you carried that model
  in from a sibling CLI, drop it.

Use `--profile/-p <name>` to target a specific server when several are configured.

## Reporting a problem — open a GitHub issue

If a task was painful, needed too many steps, hit a missing feature, or was
impossible with `drone-cli` — **and the user asks you to file it** — open an issue on
this tool's own repo. Only when asked; never unprompted; one issue per problem.

- Repo: `alexander-zierhut/agent-tool-drone-cli`
- Issues: https://github.com/alexander-zierhut/agent-tool-drone-cli/issues
- Self-contained (no repo/README needed beside the binary): `drone-cli report` prints exactly this — the repo, a pre-filled `issues/new` link, and a `gh` one-liner — offline, no token.

Search for a duplicate first, then file — prefer the `gh` CLI:

```bash
gh issue list   --repo alexander-zierhut/agent-tool-drone-cli --search "<keywords>"
gh issue create --repo alexander-zierhut/agent-tool-drone-cli \
  --title "<one-line summary>" \
  --body  "<goal · exact command(s) run · JSON error + exit code · `drone-cli --version` · what would have made it work>"
```

If `gh` is missing or unauthenticated, hand the user a prefilled link instead:
`https://github.com/alexander-zierhut/agent-tool-drone-cli/issues/new?title=…&body=…`.
