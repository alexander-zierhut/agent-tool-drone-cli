# Drone CI Agent-Ready CLI — Implementation Plan

**Target:** `/workspace/Development/zierhut-it/agent-tools/drone/`
**Model:** a direct port of the `agent-tool-openproject-cli` chassis (Typer + httpx + keyring + rich) — same testing, formatting, install, context, first-run and command conventions; GitHub + PyPI.
**Pinned API target:** Drone **2.28.2**. The live spike ran the floating `drone/drone:2` (which resolves into 2.28.x); source-level claims below were checked against the `v2.28.2` tree. Pin the concrete tag in the committed compose.

Tags used below: **verified live** = observed in the 2026-07-16 spike against a real gitea + drone + runner stack. **verified in source** = read in the v2.28.2 tree, not executed. **unverified** = neither.

---

## 1. Executive summary

The OpenProject CLI splits into a **chassis** (~60–70%: argv pre-parser, error→exit-code funnel, Emitter, retry matrix, dry-run-as-exception, config/credentials, sticky context, first-run hooks, `install claude`, the test harness) and **bodywork** (~30–40%: HAL, `lockVersion`, the filter DSL, the cost/usage report). The chassis ports almost verbatim; the bodywork is deleted and replaced.

**Simpler here:** no HAL (`hal.py` deleted outright — it is the largest single deletion), no `lockVersion` (`update_locked` and its deep-copy-per-attempt subtlety go with it), no server-side filter DSL, no `rails runner` token minting (the token is a compose constant).

**Harder here:** builds are **asynchronous** (nothing in the OpenProject chassis has a poll-until-terminal axis — this is net-new design), **logs are line-oriented text over two subsystems** (durable REST store vs ephemeral SSE), the API has **almost no server-side filtering** (so `--where` moves client-side and needs a page budget), and the **SCM is a hard dependency** (a repo cannot exist in Drone until it syncs from Gitea/GitHub).

### 1.1 How Drone differs, in ways that change the code

| Axis | OpenProject | Drone | Consequence |
|---|---|---|---|
| **Wire format** | HAL (`_links`, `_embedded`, Formattables, `customFieldN`) | plain flat JSON | Delete `hal.py`. Keep `serialize.py` — its job changes from *flattening* to *projecting + renaming + epoch→ISO*. |
| **Identity** | numeric ids | `{owner}/{name}` slug in every path; **build NUMBER, not id** (both fields exist in the JSON) | Resolver shrinks to a slug/number parser. Hard-error when a build `id` is passed where a `number` belongs. |
| **Concurrency** | `lockVersion`, 409 on stale | none; last-write-wins. Repos carry a `version` nobody accepts as a precondition | Delete `update_locked`. Exit code **6 becomes RESERVED, not renumbered** — the codes are published API. |
| **Errors** | `{"_type":"Error",…}`, 422 | `{"message":"…"}`, **400** for validation, no 422, no 409 | `_raise_for_error` simplifies; `field_errors` goes unused (keep the field, it costs nothing). |
| **Pagination** | authoritative `total` | bare JSON array, `?page=&per_page=`, **no total** | The OpenProject rule ("never stop on a short page") **inverts**. See §2.0. |
| **Async** | every call is one round-trip | `POST /builds` → `{status:"pending"}`; a runner picks it up seconds-to-minutes later | Net-new: `wait`/`watch`/`--follow`, a terminal predicate, SSE, a poll fallback, and a deliberate exit-code decision. |
| **Payload** | structured records | **logs are the point** — `{pos,out,time}` lines, N+1 per step, no build-level log endpoint | Second stream mode (`stream_lines`, raw text), Rich-markup hazard, `--tail`/`--grep`, failed-step extraction. |
| **Permissions** | one role model | **two orthogonal layers**: repo perms (read/write/admin, synced from the SCM) and system admin (`user.Admin`), which bypasses *all* repo ACL checks | Preflight `GET /api/user` and say *which* admin you lack. Several PATCH fields are **silently dropped** for non-system-admins (200 + unchanged values). |
| **Backing store** | self-contained | **SCM-dependent**: repos only enter Drone via sync; builds fetch `.drone.yml` using the **repo owner's** SCM token, not the caller's | The test stack needs Gitea. `enable` permanently sets `repo.UserID = caller`. |
| **Rate limits** | none to speak of | Drone itself has **none**; the limit that bites is the upstream SCM's, visible only at `GET /varz` | Keep the 429 arm anyway (proxies do 429). Surface `scm.rate.remaining` before sync-heavy ops. |
| **Versioning** | `/api/v3`, real drift across 13–17 | `/api` unversioned; **`/version` at the web root** | A compat matrix is worth far less. Drone is in maintenance. **Pin one version.** |

### 1.2 Traps that shape the code before line one

1. **Cron is seconds-first.** Drone uses robfig/cron v1 (`Second|Minute|Hour|Dom|Month|DowOptional`). A standard 5-field crontab `"0 3 * * *"` **parses successfully** and means `second=0 minute=3 hour=*` → **fires every hour**, not daily at 03:00. Silent 24× misconfiguration, no error. *(verified in source; independently confirmed by a second reviewer.)* The single highest-value guard the CLI can ship.

2. **Secret values are structurally unreadable.** Every read and write handler returns `secret.Copy()`, which omits `Data`. *(verified live: `GET .../secrets/{name}` returns only `{id, repo_id, name}`.)* The published docs' examples showing `"data": "octocat"` are wrong. No `secret get --show-value` can ever exist; `--dry-run` on `secret add` must redact the body it prints; `--fields data` and `--raw` must be refused on secret endpoints.

3. **The API token is not the SCM token, and Drone maps SCM auth failure to 500.** Without a populated `users.user_oauth_token`, `POST /api/user/repos` returns **HTTP 500 with body `{"message":"Unauthorized"}`** *(verified live)*. Two consequences: retry logic must **not** treat 500 as transient, and `server doctor` must be able to say "your Drone token is fine; your SCM link is dead" instead of surfacing a 500. Related: **`GET /api/version` does not exist** — 404 *(verified live)*; the real endpoint is **`GET /version`** on the web root, unauthenticated. `/healthz` answers 200 *(verified live)* but answers it **early** — it is no proof the DB migrated or the bootstrap ran *(that it performs no DB check is **verified in source**)*. The real readiness probe is `GET /api/user`, which proves server-up AND bootstrap-ran AND token-valid in one call.

4. **`build.link` is not a commit link, and it changes shape by event.** All verified live — *on Gitea 1.22; Forgejo is a Gitea fork sharing this URL scheme, but re-confirm on Forgejo 9 in Phase 0 since that is the SCM we actually run*:

   | event | `build.link` |
   |---|---|
   | `push` | `{scm}/{owner}/{repo}/compare/{before}...{after}` — a **compare/diff** page, not a commit |
   | `custom` (API-triggered) | `{scm}/api/v1/repos/{owner}/{repo}/git/commits/{sha}` — an **API** URL that renders as JSON, not a web page |

   Drone takes `link` from whatever the SCM handed it — the webhook payload for a push, the API response for an API trigger — and never normalizes. So **`build.link` is unusable as "the link to this commit"**, which is exactly what a human or an agent wants to paste. Don't pass it through.

   **The reliable derivation** (verified live: returns 200): `repo.link` is the repo's **web** URL (`http://forgejo:3000/acme/api`) and `build.after` is the commit SHA, so
   ```
   commit_url = f"{repo.link}/commit/{build.after}"      # 200 on Gitea/Forgejo + GitHub
   branch_url = f"{repo.link}/src/branch/{build.target}" # Gitea/Forgejo; GitHub differs
   ```
   **Caveat, verified live: `repo.scm` is an empty string** even on a fully synced, enabled repo — so the SCM type is *not* discoverable from the object and the URL pattern cannot be chosen from it. Gitea/Forgejo and GitHub share `/commit/{sha}`; GitLab uses `/-/commit/{sha}` and Bitbucket `/commits/{sha}` — both probed live against Gitea and **neither works there** (404 and 303 respectively), which is the point: one pattern cannot serve all SCMs, and the object won't tell you which you're on. So: default to `/commit/{sha}`, and make it **overridable per profile** rather than guessed. This feeds a `--links` flag on `build info`/`usage report --detailed` emitting `commit_url`/`repo_url`/`branch_url` — small, and the single most-pasted thing in any CI conversation.

> Two traps from earlier drafts are **false and have been removed**: Drone does *not* serve SPA HTML on a wrong `/api/…` path (it returns a plain-text `404 page not found`, *verified live*), and `drone/drone:2` is **not** an oss-tagged build (see §2.0).

---

## 2. Command surface

### 2.0 Cross-cutting contracts

**Reserved global namespace, asserted by test.**

```
--format, -o, --output    output format (json|table|markdown|csv)
--fields, --columns       dotted-path projection
--dry-run                 print the write, don't perform it, exit 0
--stream                  NDJSON (records) / raw text (logs)
--no-context              bypass sticky context for one invocation
--profile, -p             named server profile
--no-color
--version, -V             eager root global
```

Popped from **anywhere** on the line by `_pop_globals`, handed off via `DRONECLI_*` env vars. **Divergence #1:** `--profile`/`-p`, `--no-color` and `--version`/`-V` are *also* reserved, so there is one rule ("globals work anywhere") instead of OpenProject's before/after asymmetry. **`--version`/`-V` is a real eager option on opcli's root callback** (`src/opcli/cli.py:69`) and is absent from opcli's reserved set — include it here or the reservation test has a hole in the exact bug class it exists to close.

**Divergence #2 — `-f` is deliberately NOT a format alias.** OpenProject reserves `("--format","-f","--output","-o")`. Drone frees `-f` for `--follow` (`tail -f`, `docker logs -f`, `kubectl logs -f`). Format short flag is `-o` only (kubectl precedent). `-p` is claimed by `--profile`, so `build run` uses `--param` with **no** short flag.

**The reservation test is mandatory.** opcli shipped a live bug: `attach download --output /tmp/x.pdf` is eaten by the pre-parser → `OutputFormat.coerce("/tmp/x.pdf")` raises → `_resolve_format` swallows it with a bare `except ValueError: pass` → format degrades to json → `download` gets `output=None` and writes to CWD **with exit 0**. The capital `-O` in that signature is the scar of someone hitting the collision and half-fixing it. Drone collides harder (`log view --output build.txt`). So:

* `tests/test_globals_unit.py` walks the Typer tree and fails if any param's `opts`/`secondary_opts` intersect the reserved set — including `-V`.
* **File destinations are `--to PATH`** everywhere. Never `--output`.
* An unparseable **explicit** `--format` **hard-fails**. Only the env and saved-config rungs stay lenient — a typo'd `.bashrc` export must not brick every command; an explicit flag must not fail silently.

**Exit codes** (published contract — README, `guide`, SKILL.md):

| Code | Meaning |
|---|---|
| 0 | ok (including a successful `--dry-run`) |
| 1 | generic `DroneError` (incl. HTTP 402 license/seat limit, carried as `{"status":402}`) |
| 2 | usage (Click/Typer — never allocate) |
| 3 | config |
| 4 | auth — 401/403 |
| 5 | not found — 404 |
| **6** | **RESERVED / unallocated** (was OpenProject's `ConflictError`; Drone has no optimistic locking and returns 400 for uniqueness collisions). Never reuse, never renumber. |
| 7 | validation — 400 |
| **8** | **not implemented — 501** *(new)*. Not reachable on `drone/drone:2` (below), but Drone Cloud disables org secrets and an oss-tagged build compiles several groups out. One line in `errors.py`; an agent must be able to branch on "this server cannot do that". |
| 130 | SIGINT |
| **20–29** | **build-outcome band, ONLY under `--exit-code`** |

**Build status must NOT leak into the exit code by default.** Conflating "the CLI failed" with "the thing the CLI observed failed" breaks the contract agents rely on. `build wait` exits **0** when it successfully observed a terminal state and puts the status in JSON. `--exit-code` is the opt-in gate, documented as overriding the normal contract, on a band that cannot collide with the error band: `0 success · 21 failure · 22 killed · 23 error · 24 blocked(needs approval) · 25 declined · 26 skipped · 27 timed out waiting`.

**On the "oss build" question — settled, drop it.** Earlier drafts engineered heavily around `// +build !oss` files returning 501. That is **false for the image we ship**. `scripts/build.sh` compiles `cmd/drone-server` with **no build tags** and `Dockerfile.server.linux.amd64` ADDs exactly that binary as `drone/drone` — the `-tags "oss nolimit"` line in `.drone.yml` is a `-o /dev/null` compile check that is never published *(verified in source)*. And live on `drone/drone:2`, **all of these returned 200**: repo secrets (POST/GET/PATCH), org secrets, cron incl. exec, templates, promote, admin users *(verified live)*. So: **no capability probe script, no `DRONECLI_OSS_BUILD` env var, no `needs_nonoss` marker, no per-group "→ 501" annotations.** Keep exit 8 and `server doctor`'s probe; that is the entire residue. Corollary: because the published image is the non-oss, license-limited build, **HTTP 402 is a live path** (`repo enable` past the repo limit, `user create` past the seat limit) — good, it's testable.

**Pagination — the inverted scar, and the trap inside the inversion.** Drone returns a bare array with no `total`, so the terminator *must* be "empty page OR short page" — the exact heuristic `client.py`'s comment warns against. That is only sound if the server won't silently cap you. `handler/api/repos/builds/list.go:44-46` does `if limit < 1 || limit > 100 { limit = 25 }` — it **resets to 25**, it does not clamp to 100. Ask for `per_page=200`, get 25 rows, short-page test says "done", you have silently truncated at 12% with exit 0 and valid JSON. **Rule: clamp `per_page` client-side to 100 before sending.** Note the rule is *not* universal — `handler/api/repos/all.go:36-38` (the admin `repo ls --all` path) has the `>100` check commented out and honours `per_page=1000` *(both verified in source)*. Clamping client-side stays safe on both, but the >1-page fixture test must pin **both** handlers or it encodes the wrong belief for one.

`GET /api/user/repos`, `/api/users`, `.../secrets`, `.../cron`, `/api/templates/{ns}` have **no pagination at all** — full arrays.

**Client-side filtering + page budget.** No server-side filter by status/event/author/time exists. `build ls --status failure --limit 10` on a healthy repo pages the entire history. So: `--max-pages` (default 20 ≈ 2000 builds) and an explicit truncation signal — `{"truncated": true, "pagesScanned": 20}`. **"Stopped searching" must never render as "nothing found."**

**`--where` compiles to a predicate, not a wire filter.** `parse_where`, `ALIASES`, `canonical_field`, `to_date`, the `Field` dataclass and the longest-operator-first lexer port **verbatim**. Only `compile_where` changes: it returns `(item) -> bool`. Because we evaluate, we *define* the operators — drop the `>`→`>=` collapse and the `<>d` date-range rewrite; `>` is strictly greater. **Epoch hazard:** `finished == 0` means *still running*, not epoch 0, so `--where "finished > 1d"` must special-case 0-as-unset or it silently excludes every in-flight build.

**Discovery — `dronespec.py` + the trio.** The blueprint makes "prefer discovery to facts" a contract, and specifically warns that where there is no live schema endpoint the static registry becomes the *only* discoverability source. Drone is exactly that case. Ship `dronespec.py` (the `Field` registry, `ALIASES`, `OPERATORS`, enum tables) and the same three commands opcli ships as `search fields|operators|values`:

```
drone-cli fields                 # filterable fields, kind, CLI flag, description
drone-cli operators              # the operator codes
drone-cli values <field>         # allowed values for an enum field
```

Registry contents (fields as they appear on the build object): `status`, `event`, `target` (branch), `ref`, `author` (`author_login`), `sender`, `message`, `deploy_to`, `cron`, `created`, `started`, `finished`, plus `repo`/`namespace` on cross-repo commands. Enums: **statuses** `pending, running, success, failure, killed, error, blocked, declined, skipped, waiting_on_dependencies`; **events** `push, pull_request, tag, promote, rollback, cron, custom`. Of those, the spike observed live: statuses `pending/running/success/failure/killed`, events `push/custom/cron/promote` — the remainder are read from `core/build.go`, not executed. `drone-cli values status` must say so.

**The registry needs guard tests, because nothing live catches its drift.** Two, both cheap:
* *Tier 1:* every `Field.name` in the registry exists as a key on `SAMPLE_BUILD` (catches renames and typos).
* *Tier 2 (live):* page real builds, collect the distinct `status`/`event` values seen, assert each is in the registry enum. Catches drift-by-addition — the direction that actually happens.

**Presets — the zero-argument agent entry points.** opcli ships six (`search mine/reported/watching/unassigned/overdue/recent`) off one shared `_preset()`; they are the cheapest commands in the tool for an agent with no context. Because `--where` is client-side, presets here are nearly free and are the natural consumers of the page budget. Ship four, deliberately: **`build failing`** (one request — `GET /api/user/repos?latest=true`, keep rows whose latest build is `failure`/`error`), **`repo broken`** (one request — active repos whose latest build failed, plus active repos with no build at all), **`build running`** (one request — `GET /api/builds/incomplete/v2`, admin, **unverified — see §2.4; falls back to the verified `GET /api/queue`**), **`build mine`** (one request + page budget — builds where `author_login == me.login`).

**Redaction is a first-class concern.** `serialize.secret()` emits `{name, pull_request, pull_request_push}` and **never** a value field — do not rely on the server blanking it. `--fields data` is denylisted (`_dotted_get` will happily walk to any key present). `--raw` is refused on secret endpoints — `--raw` exists to bypass the serializer, which is exactly the redaction it would bypass. `--dry-run` redacts `data` in the printed body. Put `redact: bool` in the registry, not in ad-hoc call-site checks.

---

### 2.1 `auth`

| Command | Endpoint | Notes |
|---|---|---|
| `auth login [--server URL] [--token T] [--profile P] [--no-verify-ssl]` | `GET /api/user` | **Paste-a-token flow only** — there is no credential→token exchange in Drone. Verify **before** persisting; assert the response is JSON with a `login` key (justification: you pointed at a non-Drone server, not Drone's own routing). Backfill `username` from `me["login"]` — Drone's identity field is literally `login`, so the opcli line works unchanged. |
| `auth logout [--profile P]` | — | Purges the token from **every** backend (env note, keyring, 0600 file). |
| `auth status` | `GET /api/user` | **Degrades, never fails** (exit 0 even when unconfigured/unreachable) — it is the command you run when things are broken. Reports `credentialBackend`, `hasToken` (never the value). |
| `auth whoami` | `GET /api/user` | Renders `last_login` as "3 hours ago", badges **machine** vs human and **admin**. Live shape: `{id,login,email,machine,admin,active,avatar,syncing,synced,created,updated,last_login}` — note the server emits `avatar` while the docs and drone-go say `avatar_url`; coalesce both. |
| `auth token show` | `POST /api/user/token` | The only way to read your own token programmatically. Confirmation-gated, redacted by default (`--reveal`). |
| `auth token rotate` | `POST /api/user/token?rotate=true` | Returns the plaintext new token. **Footgun with no undo** — confirm-gated, `--dry-run`-able, states "this invalidates your current token everywhere", atomically re-saves into the profile. |

**Auth mechanics.** `Authorization: Bearer <token>` — swapping `httpx.BasicAuth("apikey", token)` for a bearer header is the only change in `Client.__init__`. `api_root = base.rstrip("/") + "/api"` — the single place `/api/v3` appeared. Timeout 60s → 30s (only log fetch is slow). The token is `user.Hash`: 32-char random, **never expires, exactly one per user, no scopes, no named tokens, no revoke-one-of-many.** `credentials.py` copies **verbatim** — the env→keyring→0600-file cascade, the `fail.Keyring` isinstance check (keyring silently installs a *fail* backend on headless boxes rather than raising), the `os.open(…, O_CREAT, 0o600)` write, the delete-from-every-backend logout, `backend_name()`.

**The auth model has a hole worth documenting.** `handler/api/api.go:173-178` adds `acl.AuthorizeUser` to `/repos` **only** when `DRONE_SERVER_PRIVATE_MODE == "true"` *(verified in source)*. On a default server, **public repos are readable with no token at all** — `repo info`, `build ls`, `build info`, `log view` can all return 200 unauthenticated. So `/api/user` is the *only* valid token probe (it is always behind `AuthorizeUser`), and Tier-2 permission tests against a public demo repo do not exercise the 403 paths they appear to. One line in the auth guide topic.

**Env vars — read the ecosystem's names, namespace our own.** `DRONE_SERVER` + `DRONE_TOKEN` are what the official CLI reads and every tutorial exports; read them. But a Drone CLI very often runs **inside a Drone pipeline**, where the runner injects `DRONE_REPO`, `DRONE_BRANCH`, `DRONE_BUILD_NUMBER`, `DRONE_COMMIT_SHA`… So: `DRONE_SERVER`/`DRONE_TOKEN` for compat, `DRONECLI_SERVER`/`DRONECLI_TOKEN` as higher-precedence aliases, and **`DRONECLI_*` for everything this CLI invents** (`CONFIG_DIR`, `FORMAT`, `CLI_FORMAT`, `CLI_FIELDS`, `DRY_RUN`, `STREAM`, `NO_CONTEXT`, `PROFILE`, `SECOND_TOKEN`). OpenProject never faced this because nothing else on the box sets `OPCLI_*`.

### 2.2 `settings` / `context` / `guide` / `install` / `raw`

All five port near-verbatim. **`settings`** (`show`/`set-format`/`get-format`/`path`) keeps reload-before-save — the whole document is serialized on save and first-run prompts write to it mid-process. **`context`** keeps the verbs, gets new keys (§3) and **provenance in `show`**. **`guide [topic]`** keeps the OVERVIEW+TOPICS registry; unknown topic → list + overview + exit 2. **`install claude`** is a find-and-replace (`claude_available()` is unchanged — it detects *Claude*, not the target tool; markers become `<!-- drone-cli:start -->`). **`raw get|post|patch|put|delete`** is verbatim and **more valuable here** — the API is thinly documented and `raw` is how an agent verifies an endpoint exists.

### 2.3 `repo`

| Command | Endpoint | Notes |
|---|---|---|
| `repo ls [--all] [--active/--inactive] [--namespace X] [--search Q] [--with-latest]` | `GET /api/user/repos` (`?latest=true`) · `GET /api/repos` (admin) | `--with-latest` → the **undocumented `latest=true`**, which fills the last-build column in *one* request instead of N+1. All filtering/sorting client-side. |
| `repo info [REPO]` | `GET /api/repos/{o}/{n}` — 200 | The only repo GET returning `permissions {read,write,admin}`. 404 before activation. |
| `repo enable [REPO] [--sync]` | `POST /api/repos/{o}/{n}` — 200 | repo-admin. **`--sync` first** so a brand-new SCM repo works in one command instead of a confusing 404. **Warn on the implicit chown**: enable sets `repo.UserID = you`, silently moving every webhook/clone/config-fetch onto *your* SCM token. Defaults `config_path=.drone.yml`, `timeout=60`. Idempotent (re-enabling re-chowns). **402 → "repo limit reached (N/N active)"**, live numbers from `/varz`. |
| `repo disable [REPO]` | `DELETE /api/repos/{o}/{n}` | Sets `active=false`. **Correction to the docs and to common belief: this does NOT remove the SCM webhook** — `HandleDisable` takes no `HookService`. |
| `repo rm REPO --yes` | `DELETE …?remove=true` | Hard-deletes the row. **Required positional + `--yes`; in the context skip set.** Returns on the next sync — there is no "ignore forever". |
| `repo sync [--wait]` | `POST /api/user/repos` | **Synchronous by default** (returns the refreshed list). `?async=true` → bare 204 with no completion signal → `--wait` polls `GET /api/user` until `syncing` flips false. **This is the call that fails with 500 `{"message":"Unauthorized"}` when the SCM token is missing** *(verified live)* — special-case it into "your Drone token is valid but your SCM link is dead; run `repo repair` or re-login to the SCM". |
| `repo chown [REPO]` | `POST …/chown` — 200 | **Only to yourself** — no target param exists. Use when the previous owner's SCM token died. |
| `repo repair [REPO]` | `POST …/repair` — 200 | Recreates the webhook **and** re-syncs metadata from the SCM, acting as the repo **owner**, not the caller. Returns nothing observable → re-fetch and report, don't print a hollow "Success". |
| `repo update [REPO] --visibility --config-path --protected --ignore-forks --ignore-pull-requests --auto-cancel-* --timeout --throttle --trusted --counter` | `PATCH /api/repos/{o}/{n}` — 200 (`{"timeout":90,"protected":true}` verified live) | **12 fields; the docs show 5.** See the silent-drop guard below. Validate `visibility ∈ {public,private,internal}` **client-side**: the server's `govalidator.IsIn` check is **commented out**, so `{"visibility":"pubic"}` is accepted, persisted, and returned 200. |
| `repo harden` / `open` / `quiet` | ↑ | Named-intent presets. `harden` = protected+private+ignore_forks, **never** touches `trusted`. |
| `repo doctor [REPO]` | composite | active? config_path present? owner still an active Drone user? last build recency? → suggests `repair`/`chown`. |
| `repo collab ls\|get\|rm` | `GET/DELETE …/collaborators[/{member}]` | Read = repo read; `rm` = repo admin, 204. **Asymmetric shapes:** `ls` returns `[]Collaborator` (login, avatar, read/write/admin…), `get` returns `core.Perm` = **`{read,write,admin}` only**. Merge them so `get` is a superset of a `ls` row. `rm` is a **repair tool**, not membership management: it deletes a stale local perm row, does not revoke SCM access, and is undone by the next sync. Say that in the confirmation. |

**The silent-drop guard (highest-value code in `repo`).** `trusted`, `timeout`, `throttle`, `counter` are gated on `if user != nil && user.Admin` — **system** admin, not repo admin — and are **silently dropped** otherwise: HTTP 200, old values, no warning *(verified in source)*. `repo update` **must diff the requested fields against the returned object** and fail loudly:

```
error: server accepted the request but did not apply --timeout
       (--timeout/--throttle/--trusted/--counter require system-admin;
        you are a repo-admin on octocat/hello-world but not a Drone admin)
```

Preflight `GET /api/user` once to learn `admin` and refuse up front. Additionally: `--trusted` is a **privilege-escalation** switch (privileged containers, host mounts) → interactive confirmation or `--i-understand-this-grants-privileged-containers`, never set by a preset. `--counter` (next build number) is gated behind `--unsafe` **and** pre-flighted against the latest build number — lowering it collides with existing rows.

**Not supported:** `repo create` (Drone mirrors from the SCM; the doc page at `/api/repos/repo_create/` means *enable*), collaborator add/update (perms sync from the SCM), `archived` writes, webhook introspection (you can `repair` a hook but never read its state), reading `signer`/`secret` (`json:"-"`).

### 2.4 `build` — the centre of gravity

| Command | Endpoint | Notes |
|---|---|---|
| `build ls [REPO] [--branch B] [--status S] [--event E] [--since 7d] [--author A] [--where EXPR] [--limit N] [--max-pages M] [--stages]` | `GET .../builds?page=&per_page=&branch=&tag=` | Only `branch`/`tag`/`page`/`per_page` are server-side (**and undocumented** — the docs say "no query parameters"). Everything else is a client-side predicate under a page budget. `--stages` fans out one `build info` per row (list **strips `stages`**) — bounded concurrent pool + progress, or it looks like a hang. |
| `build info [REPO] <N> [--raw]` | `GET .../builds/{number}` | **Embeds `stages[].steps[]` — one GET is the whole tree** *(verified live)*. The only shape carrying it. |
| `build last [REPO] [--branch B]` | `GET .../builds/latest` | Undocumented; registered *before* `/{number}` so `latest` isn't Atoi'd. Returns stages. |
| `build run [REPO] [--branch B] [--commit SHA] [--message M] [--param k=v]…` | `POST .../builds?branch=main` — 200 | Write access. **Always `event=custom`** *(verified live)* — the API cannot synthesize push/tag/pull_request/cron. **`--dry-run` must warn if `.drone.yml`'s `trigger.event` omits `custom`** — the #1 cause of "my build did nothing". Params are query-string only. Reserved keys `access_token`/`commit`/`branch` are stripped; **`message` and `action` are consumed as fields *and* still leak into params**. Bad branch/commit → **404**, not 400. |
| `build restart [REPO] <N> [--param k=v] [--debug] [--follow]` | `POST .../builds/{N}` | **Creates a NEW build number** — restarting #1 produced #2 *(verified live)*. Reuses the original's event/ref/target/deploy/params. **Param precedence is INVERTED vs promote: the previous build's params overwrite yours** (`retry.go:93-107` vs `promote.go:79-94`, *verified in source*). `restart -p FOO=new` where the old build had `FOO` **silently keeps the old value** — detect and say so. 400 on blocked/declined builds. |
| `build cancel [REPO] <N>` | `DELETE .../builds/{N}` — 200 | **Docs say "requires administrative privileges" — wrong.** The router uses `acl.CheckWriteAccess()`. |
| `build promote [REPO] <N> --to ENV [--param k=v]` | `POST .../builds/{N}/promote?target=prod` — 200, event `promote` *(verified live)* | Write. `--to` required → **pre-flight client-side**, don't surface a bare 400 "Missing target environment". Params: previous first, **yours overlay (new wins)** — opposite of restart. |
| `build rollback [REPO] <N> --to ENV` | `POST .../builds/{N}/rollback?target=` | **Entirely undocumented** — no doc page exists, yet the route, the drone-go method and drone-cli all do. Byte-identical to promote except `Event=rollback`. |
| `build approve [REPO] <N> [--stage S] [--all]` | `POST .../builds/{N}/approve/{stage}` | Exists; **400 when the build isn't actually blocked** *(verified live)*. **THE DOCS ARE WRONG**: they document a stage-less `/approve` that does not exist in any Drone version, and claim write access — it needs **ADMIN**. `--all` loops the blocked stages (no bulk API). Returns no body → re-read afterwards. |
| `build decline [REPO] <N> [--stage S]` | `POST .../builds/{N}/decline[/{stage}]` | **Asymmetric with approve**: decline *does* have a build-level route. Both need ADMIN. |
| **`build wait [REPO] <N> [--timeout 30m] [--exit-code]`** | SSE `GET /api/stream` + poll fallback | **Net-new. See §4.** |
| **`build watch [REPO] <N>`** | ↑ + log SSE | Live stage/step tree with elapsed times; exits naming the failed step. |
| **`build debug [REPO] <N> [--tail 80]`** | composite | **See §4.** |
| `build branches [REPO]` / `build pulls [REPO]` / `build deployments [REPO]` → alias `deploy status` | `GET .../builds/{branches,pulls,deployments}` | All undocumented. `deployments` is the natural companion to promote/rollback: **"what is live where right now"** — nothing else surfaces it. **Each has an undocumented DELETE sibling** (`acl.CheckWriteAccess`, *verified in source*) — either ship them or say explicitly they exist and we refuse; an agent that infers "read-only" from us would be wrong. |
| `build purge REPO --before N --yes` | `DELETE .../builds?before=N` | Undocumented, destructive, ADMIN. Required positional + typed confirmation showing the count and date range (bare 204, no feedback). **Correction: it does NOT cascade to logs** — `store/build/build.go Purge` deletes builds/stages/steps only; log blobs are **orphaned**. Worse, the orphaned stage/step cleanup is gated on `Postgres \|\| MySQL` — **on SQLite only `builds` rows are deleted** *(verified in source)*. Say all of it in the confirmation. |
| `build running [--all]` / `build blocked` | `GET /api/builds/incomplete/v2` (admin) — **UNVERIFIED** | Undocumented, and **neither the spike nor the source review confirmed it** — probe it in Phase 0 before building on it. Believed to return `[]RepoBuildStage`; v1 (`/incomplete`) returns `[]Repo`+embedded build — normalize both so agents never learn the difference. Fallback if absent: `GET /api/queue` (admin, verified 200 **[live]**) returns incomplete stages and covers the same need. |
| `feed` | `GET /api/user/builds` — 200 | **SHAPE TRAP**: despite the name it returns **repository** objects (latest build per repo), not builds *(verified live)*. Normalize. The source registers `/builds/recent` as an identical alias with a TODO saying the name isn't final — expose exactly one path. |

**`build wait`'s terminal predicate deliberately deviates from `IsDone()`.** `core/build.go:121-131` says not-done while `waiting_on_dependencies|pending|running|blocked`. Copying that *exactly* makes `wait` loop on every approval-gated pipeline until `--timeout` — precisely the bug the feature exists to prevent. **Terminal = `IsDone() || status == "blocked"`**, and `blocked` is reported as a distinct outcome: *"build 42 is blocked on stage 2 (deploy) awaiting approval — run `drone-cli build approve 42 --stage 2`"*. Write the deviation down in the code, or someone will "fix" it back.

### 2.5 `log`

| Command | Endpoint | Notes |
|---|---|---|
| `log view [REPO] <N> [--stage S] [--step T] [--tail K] [--grep RE] [--to PATH] [--raw]` | `GET .../builds/{N}/logs/{stage}/{step}` → JSON array of `{pos,out,time}` *(verified live)* | **Stage/step are 1-indexed NUMBERS** — the docs' own curl (`/logs/1/logs/default/0`) cannot work. **No stage/step → fan out over the whole build**, section-headed. Resolve `--step "go test"` by *name* from the build object, client-side. |
| `log follow [REPO] <N> [--stage S] [--step T]` (`-f` on `view`) | REST backfill + SSE `GET /api/stream/{o}/{n}/{N}/{stage}/{step}` | **Two subsystems**: LogStore (durable, what REST reads) vs LogStream (ephemeral pubsub, what SSE reads). See the SSE note below. |
| **`log failed [REPO] <N> [--tail 80] [--context 20]`** | composite | **See §4 — the #1 agent feature.** |
| `log purge REPO <N> <stage> <step> --yes` | `DELETE .../builds/{N}/logs/{s}/{t}` → **204; the log then 404s** *(verified live)* | ADMIN. Undocumented (only the `drone log purge` *command* is documented). |

**Log mechanics.** Line = `{pos, out, time}`. `out` **already includes its trailing `\n`**. `time` is **whole seconds since step start**, not an epoch — 1-second granularity, no wall clock; correlate via the step's `started`. Rendering: write raw `out` verbatim to `sys.stdout`, **bypassing the Rich Console** — `Console.print` interprets `[...]` as markup and CI logs are full of `[INFO]`, `[error]`, `[0;31m`. `highlight=False` does *not* protect against this. OpenProject never hit it because work-package subjects rarely open with a bracket; Drone logs hit it on day one.

**404/400 disambiguation.** `HandleFind` 404s when the repo, build, stage, step *or the log blob* is missing — so "no such build", "step still queued" and "logs were purged" collapse into one bare 404 (`{"message":"sql: no rows in result set"}` for a never-ran step, *verified live*). Distinguish client-side by first reading the build. **But** `logs.HandleFind` runs `strconv.Atoi` on `{stage}`/`{step}` **before** any lookup and returns `render.BadRequest` → **400**, not 404, on a non-numeric segment *(verified in source)* — so the disambiguation helper must not be pointed at the wrong status, and `--step "go test"` must resolve client-side or it produces a 400 that maps to exit 7.

**Defensive decoding.** `find.go` `io.Copy`s the stored blob through with `Content-Type: application/json` and carries a maintainer TODO saying logs are stored jsonl and need conversion. In practice the runner uploads a marshalled array (drone-go decodes `[]*Line`, and the spike read a real array back), but **sniff array-vs-JSONL** rather than assume.

**Streaming needs a second mode.** `stream_json` (NDJSON, one object per line, explicit per-line `flush()` — stdout is block-buffered when piped, so without it laziness is invisible to `jq`) is right for `build ls`. Logs need `stream_lines`: raw `out` text. And **a streaming read must NOT be wrapped in the retry loop** — retrying a half-consumed stream replays lines the caller already saw. New failure mode; no OpenProject endpoint streams.

**SSE — both streams are real and undocumented** *(verified live)*.
* `GET /api/stream` — global feed. Emits `: ping`, then `data: {repo json}` on build state changes. This is what drives `build wait` without a poll storm.
* `GET /api/stream/{o}/{n}/{build}/{stage}/{step}` — live log tail. Emits `: ping`, log lines, then terminates `event: error` / `data: eof`.
* **`event: error` with body `eof` is the NORMAL end-of-stream marker.** A naive SSE client reports failure on every successful stream. Pin it in a unit test.
* `curl -I` returns 405 — HEAD is unsupported; you must GET.
* **A RUNNING step DOES replay buffered history to a new subscriber** (`livelog/stream.go:59-71 subscribe()` replays `s.hist`); only a **finished** step yields an immediate eof (`handler/api/events/logs.go:98-102` returns a nil errc) *(verified in source)*. So REST backfill + SSE follow overlap **by guarantee, not by accident** — dedupe on `pos` is mandatory, and do not write a test asserting "SSE yields no history for a running step".

### 2.6 `secret` (repo) and `orgsecret` (org)

Two scopes, **two different URL trees, two different stores, two different ACLs** — not one resource with a scope flag. Hide it behind one mental model: same verbs, `--org X` flips the tree.

| Command | Endpoint | ACL |
|---|---|---|
| `secret ls\|get [REPO] [NAME]` | `GET /api/repos/{o}/{r}/secrets[/{name}]` — 200 | repo **write** |
| `secret add [REPO] NAME [--from-stdin\|--from-file F\|--from-env V\|VALUE] [--allow-pull-request] [--allow-push-on-pull-request]` | `POST .../secrets` — 200 (not 201) | repo write |
| **`secret set`** (idempotent upsert) | probe `GET` → `POST` or `PATCH` | The **biggest ergonomic win**: the API forces a create-404-then-patch dance with no clean 409. One declarative verb. |
| `secret update [REPO] NAME …` | `PATCH .../secrets/{name}` — 200 | pointer-based partial; **name cannot change** (no rename → delete+recreate, which needs the value you can't read) |
| `secret rm [REPO] NAME --yes` | `DELETE .../secrets/{name}` — 204 | |
| **`secret audit [REPO]`** | composite + local `.drone.yml` | **See §4.** |
| `orgsecret ls\|get\|add\|set\|update\|rm --org NS` | **`/api/secrets/{namespace}`** — POST/GET 200 *(verified live)* | read = org **membership**; write = org **ADMIN**. Also disabled on Drone Cloud. |
| `orgsecret ls --all-orgs` | `GET /api/secrets` | **system admin**. Undocumented. Grouped-by-namespace inventory; nothing else in the ecosystem surfaces this. |

**Org secrets are at `/api/secrets/{namespace}`, NOT `/api/orgs/{ns}/secrets`** — the latter 404s, verified live even against a real org (the intuitive path is the wrong one, so expect to re-learn this).

**Never take a secret value as argv by default** — `ps` and shell history. `--from-stdin`/`--from-file`/`--from-env` and an interactive no-echo prompt are the defaults; a bare positional is accepted but warned.

**`--allow-pull-request` on `yaml secret add` (the *encrypt* path) is a documented no-op** — the `/encrypt` handler reads only `in.Data` and silently ignores the flags, even though drone-cli exposes them. Warn, or you teach a false security model. On the **DB-backed** `secret add` path they are honoured.

Validation, client-side (mirror `core.Secret.Validate`): name non-empty, matches `[a-zA-Z0-9-_.]+`, data non-empty (you **cannot** create an empty secret).

**Teach "impossible" well.** An agent will try `secret get --show-value` or `secret copy repoA repoB`. Fail fast with the reason ("values are write-only server-side; use `secret set --from-env`"), and surface `{"data": null, "note": "values are write-only"}` — **never `""`**, which reads as "the secret is blank". **Not supported at all:** reading values, copy-between-repos, export, value drift-detection, rotation/versioning/history/expiry (the `Secret` model has **no created/updated fields**), rename, "which builds used this secret", external plugins (Vault/AWS SM resolve at build time via the extension protocol, invisible to CRUD). The `type` field is decoded and discarded — don't expose it.

### 2.7 `cron`

| Command | Endpoint | Notes |
|---|---|---|
| `cron ls\|info [REPO] [NAME]` | `GET .../cron[/{name}]` — 200 | **Needs repo WRITE** — read-only access cannot even *list* crons. A 403 here does not mean "no crons exist". Object: `{id,repo_id,name,expr,next,prev,event,branch,disabled,created,updated,version}` *(verified live)* — the docs' examples print **`pref`**, a typo; the wire field is **`prev`**. |
| `cron add [REPO] NAME (--expr E \| --at "3am daily" \| --every 15m \| --preset nightly) --branch B` | `POST .../cron` — 200 (not 201) | Name is **slugified server-side** ("Nightly Build" → `nightly-build`) → echo the server's name back. `event` is force-set to push and any body `event` ignored; the *builds* it produces carry `event="cron"`. |
| `cron update [REPO] NAME [--branch] [--target] [--expr] [--name]` | `PATCH .../cron/{name}` (+ emulation) | **The server's `cronUpdate` struct is `{branch, target, disabled}` ONLY.** `name` and `expr` are **silently discarded** — the docs' own example sends them and gets a 200 with the schedule unchanged. drone-cli has no `cron update` at all. **We emulate `--expr`/`--name` via DELETE+POST**, warning that `id`/`prev` history resets. The handler also **ignores JSON decode errors** — a malformed body is a 200 no-op *(all verified in source)*. Diff the response; never report a lying success. |
| `cron enable\|disable [REPO] NAME [--all]` | `PATCH …{disabled}` | The only thing PATCH really supports. |
| `cron rm [REPO] NAME --yes` | `DELETE .../cron/{name}` | |
| `cron trigger [REPO] NAME [--follow]` | `POST .../cron/{name}` — **execute now**, creates a build with event `cron` *(verified live)* | Returns the **created build object** — which drone-go throws away (`c.post(uri, nil, nil)`). We surface the number and can `--follow` it. Triggers the **current HEAD** of the cron's branch. |
| **`cron next [--expr E] [-n 5]`** | — | Local preview of the next N fire times in local TZ. **The API cannot do this**; `next` is only computed after the cron is persisted. |

**The seconds-first guard.** See §1.2. Concretely: a 5-field expr → **warn loudly**, show what it actually means, offer the seconds-prefixed correction; always print the next 5 fire times before creating (`--dry-run` and interactively); accept the descriptors (`@daily`, `@hourly`, `@every 1h30m`) and `--at "3am daily"` / `--every 15m` so agents never hand-assemble a 6-field string.

### 2.8 `template`

Org/namespace-scoped, keyed `(namespace, name)`. **The docs repeatedly say "requires write access to the repository" — wrong**; templates are org-scoped: reads = `CheckMembership(orgs,false)`, writes = `CheckMembership(orgs,true)` (org admin). Unlike secrets, `data` **is** returned.

| Command | Endpoint |
|---|---|
| `template ls --org NS` | `GET /api/templates/{ns}` — 200 *(verified live)* |
| `template ls --all` | `GET /api/templates` — **effectively system-admin-only.** Its `CheckMembership` middleware reads a `{namespace}` URL param the route doesn't have → empty string → admins bypass unconditionally, everyone else gets `Membership(ctx, user, "")` → error → 403 *(verified in source)*. Document it that way. |
| `template info\|update\|rm --org NS NAME` | `GET\|PATCH\|DELETE /api/templates/{ns}/{name}` (a `PUT` alias is bound to the same handler — **not** a full replace; prefer PATCH) |
| `template add --org NS NAME --from-file F` | `POST /api/templates/{ns}` — body `{name, data}`; namespace from the path. **Bare `POST /api/templates` → 405** *(verified live)*. |
| **`template push --org NS ./templates/ [--dry-run]`** | dir → N creates/updates, diffed first |
| **`template pull --org NS --to ./templates/`** | round-trips `data` into editable, git-versionable files |
| **`template where-used --org NS NAME`** | scans repos' `.drone.yml` for `load:` — the question the API can't answer |

**Undocumented extension whitelist** — `.yml .yaml .star .starlark .script .jsonnet`, else 400 "Template extension invalid". Validate client-side. **Rename is impossible** (`name` is path-only, ignored in the body). **`namespace` in a PATCH body silently MOVES the template to another org** — never map a `--namespace` flag to the body implicitly; require an explicit `template move` with confirmation. `template rm` has no cascade/refuse for repos still `load:`-ing it → warn via `where-used`.

### 2.9 `user` (admin)

`/api/user` (singular = **self**) vs `/api/users` (plural = **admin**) is a one-character path difference guarding entirely different privilege levels. Hide it: `auth whoami`/`auth token …` for self, `user …` for admin.

| Command | Endpoint | Notes |
|---|---|---|
| `user ls [--filter admin\|machine\|inactive] [--sort last-login] [--stale 90d]` · `user info LOGIN` | `GET /api/users[/{user}]` — 200 | ADMIN. **No pagination, no filter, no sort, no search** — the handler ignores every query param. All client-side. |
| `user create-machine NAME --token-out PATH` | `POST /api/users` — 200 | The **only chance to capture a machine token** — the response includes `token` **iff `machine:true`**, and only at creation. Lose it → delete+recreate. `active` in the body is **ignored** (hardcoded true). 402 on seat limit. Non-machine creates hit the **SCM** (`service.FindLogin`) and may overwrite login/email. |
| `user block\|unblock LOGIN` · `user rm LOGIN --yes` | `PATCH` / `DELETE /api/users/{user}` | ADMIN. **Only `{admin, active}` are decoded** — `email` is silently ignored. `--dry-run` must print the blast radius: **`active:false` also force-clears admin AND asynchronously transfers that user's repo ownership.** `rm` transfers repos + fires a webhook; prefer `block`. |
| `user rotate-token LOGIN` | `POST /api/users/{user}/token/rotate` | ADMIN. **Undocumented WART: the new token is NOT returned** (`Hash` is `json:"-"`). An admin can *invalidate* but never *learn* the replacement. Warn up front. |
| `user repos LOGIN` | `GET /api/users/{user}/repos` | ADMIN. **Correction: an access-permissions join, not ownership** (`repos.List(ctx, user.ID)` → `INNER JOIN perms`). Don't call it "owned". |

### 2.10 `queue` / `server`

| Command | Endpoint | Notes |
|---|---|---|
| `queue ls [--watch]` | `GET /api/queue` — 200 | ADMIN. Returns incomplete **stages** (`[]core.Stage`), not builds. |
| `queue pause` / `queue resume` | **`DELETE`** / **`POST`** `/api/queue` — 204 | **Inverted verbs** — DELETE=pause, POST=resume, and DELETE does *not* delete queue items. Hide it entirely behind the two named verbs. |
| `server version` | **`GET /version`** (web root, **unauthenticated**) — 200 *(verified live)* | Not `/api/version` (404). `{source, version, commit}`, all omitempty. The correct reachability/compat probe. |
| `server license` | **`GET /varz`** (web root) — 200 **[live]** | `{scm:{url,rate:{limit,remaining,reset}}, license:{kind,seats,seats_used,…}}`. *Probe it with an admin token in Phase 0 and confirm its ACL before shipping any "this endpoint is unauthenticated" warning — the spike only ever called it authenticated, so its guest behaviour is **unverified**.* `/api/system/license` and `/api/system/limits` are **commented out of the route table**; `/varz` is the only path to license data. |
| `server stats` | `GET /api/system/stats` | ADMIN. `{users, repos, builds:{pending,running,total}, pipelines:[…], events, streams, watchers}`. |
| **`server doctor`** | `/version` → `/api/user` → `/varz` → capability probe | **See §4.** |

### 2.11 `yaml`

| Command | Endpoint | Notes |
|---|---|---|
| `yaml lint [FILE] [--trusted]` | **none — 100% client-side.** `POST .../lint` is a **404; it does not exist** *(verified live)* | Report honestly that it only lints `type: docker` resources (exec/k8s/ssh are parsed and **silently skipped** — print "linted 2/5 pipelines (3 non-docker skipped)"). A `--trusted/--untrusted` diff answers "will this break on an untrusted fork?" |
| `yaml sign [REPO] [FILE] [--save] [--check]` | `POST .../sign` — exists (400 on a bad payload, *verified live*) | Write access. Body `{data: <raw yaml>}` → `{data: <hmac>}`. The **server does not touch the YAML** — the CLI strips existing `kind: signature` documents and re-appends `kind: signature`/`hmac:` **last**. **Refuse `.drone.star`/`.drone.jsonnet`** — producing an hmac that can never validate is worse than an error. Signing only matters when the repo is `protected`; failure **blocks pending approval**, it does not hard-fail. |
| **`yaml verify [REPO] [FILE]`** | synthesized — `POST .../verify` is a **404; it does not exist** *(verified live)* | **See §4.** |
| `yaml secret add [REPO] NAME --from-stdin` | `POST .../encrypt` — exists, 400 on a bad payload **[live]** | AES-GCM + base64 → splice a `kind: secret` resource → **re-sign**. Use the **bare** `/encrypt` path: that is the one the spike actually exercised. `/encrypt/secret` is registered on the same handler **[src]** but is **unverified** — don't make it primary. |
| `yaml explain [FILE]` | none | Local: pipeline inventory, types, `from_secret:` references, signature present/current, `depends_on` graph. Zero API calls; orients an agent in an unfamiliar repo. |

### 2.12 What the API cannot do — this list is agent-facing

| Thing | Why |
|---|---|
| `POST .../lint`, `POST .../verify` | **404 — no route exists** *(verified live)*. drone-go declares `pathVerify` + `Verify()`: dead client code. We synthesize both client-side. |
| `GET /api/version` | **404** *(verified live)*. Use `GET /version`. |
| `/api/nodes` | **404** *(verified live)*. drone-go declares `pathNodes`/`Node`/`NodeList`; no server route (only vestigial migrations). Do not ship `node` commands. |
| `/api/system/{license,limits}` · `/api/servers` | Routes **commented out** — use `/varz`. `/api/servers` is a **different daemon** (drone-autoscaler) at its own address; out of scope. (Prometheus lives at **`/metrics` on the web root — not `/api/metrics`** — and 401s **[live]** unless `DRONE_PROMETHEUS_ANONYMOUS_ACCESS=true`.) |
| Build-level `approve` | Documented, does not exist. Stage-level only. |
| `repo create`, collaborator add/update, secret value reads | Drone mirrors repos and perms from the SCM; secret values are write-only by design. |
| `yaml fmt` / `yaml convert` | Deprecated **no-ops** upstream (`drone fmt` is `Hidden:true` and copies bytes to STDERR unchanged; `drone convert`'s usage string says `<deprecated. this operation is a no-op>`). **Not shipped.** |
| Server-side lint / config preview | No endpoint asks "what would this YAML become after template/starlark/jsonnet expansion?" — that pipeline runs only inside the build trigger. Debugging a converter means triggering a real build. |
| Extension management | Conversion/validation extensions are **outbound webhooks** configured by env at boot. Invisible and unmanageable via the API. |
| Artifacts / test reports / annotations | No API of any kind. |
| Re-run one failed stage | `restart` always creates a whole new build. |
| **Duration, cost, credit, queue-time** | **None exist anywhere in the API** — not on builds, stages or steps; only raw epochs. Everything in §4's `usage report` is derived client-side. This absence *is* the killer feature's reason to exist. |
| ETags / conditional requests | The API router applies `middleware.NoCache`. Polling always costs a full response. |

**OpenProject command groups with NO Drone counterpart.** Both Claude skills can be installed side by side, so an agent *will* carry the OpenProject mental model across and ask for `drone-cli notifications list`. One line each in the gotchas topic pre-answers the whole class:

> Drone has **no** comments, **no** notifications, **no** wiki, **no** custom fields, **no** attachments/file links, **no** members (perms sync from the SCM), and **no** time logging. It is a build system, not a tracker. The nearest analogue of "log time" is that build minutes are *derived* — see `drone-cli usage report`.

### 2.13 `guide` topics

`repos · builds · logs · secrets · crons · templates · users · output · auth · context · costs · gotchas`

The **gotchas** topic writes itself: secrets are write-only (an agent that reads back to verify gets nothing — that is not a failure); a new SCM repo is invisible until `repo sync`; `build run` is always `event=custom` so `trigger.event` must include it; `restart` mints a **new build number** and the *previous build's* params win; `promote` needs `--to` and creates a new build; a repo must be enabled before builds exist; crons are seconds-first; `approve` is per-stage and needs admin; `/version` not `/api/version`; build **number** ≠ **id**; stage/step are **1-based ordinals**; logs are **text, not JSON**.

**The output contract needs a stated exception.** "stdout is JSON — parse it" has a hole the moment logs exist. The OVERVIEW must say plainly: *logs stream raw text; everything else is JSON*, or agents will `json.loads` a log dump and crash. OpenProject never needed this carve-out.

---

## 3. Context and first-run

### 3.1 What context means for Drone

Context pays off **more** here than in OpenProject: nearly every endpoint is `/api/repos/{owner}/{name}/…`, so a sticky repo removes the most-repeated typing in the tool.

**`KNOWN_KEYS = ["repo", "owner", "branch"]`** — `repo` is a single `owner/name` **slug**, not two keys. Drop all seven of opcli's (`project`, `user`, `assignee`, `author`, `status`, `priority`, `query`) — not one of them has a Drone counterpart worth making sticky. Deliberately **not** sticky: `build` (a pinned number aging into staleness is a footgun — build is the one value users always mean freshly) and `status`/`event` (a sticky `--status failure` silently hides passing builds). `owner` exists separately for the org-scoped tree (`orgsecret`, `template --org`) and defaults from `repo`'s namespace when unset.

**The positional-vs-option decision, made deliberately.** `_context_default_map` only injects into params where `param_type_name == "option"` — a load-bearing safety refusal, not style. Click's `default_map` *does* satisfy required positionals; without the filter, `context set --project webshop` turns a bare `openproject project delete -y` into silent destruction (verified experimentally in opcli). Upstream drone-cli uses positionals (`drone repo info <repo>`), so a sticky `--repo` would **silently do nothing** against positional-style commands — the feature would look broken with no error.

**Decision:** repo is an **option** (`--repo owner/name`) *plus* an **optional leading positional** that falls back to it. Resolution ladder:

```
positional REPO > --repo (explicit) > saved context.repo > $DRONE_REPO (CI-injected)
                > git remote autodetect (cwd) > error naming all four
```

`$DRONE_REPO` sits **below** saved context (a human who ran `context set --repo x` meant it) and **above** nothing, so the CLI "just works" unconfigured inside a pipeline — a rung the OpenProject chassis has no equivalent of.

**Destructive-command carve-out.** The option-filter protects required positionals, not options a destructive verb then consumes. So: **a command whose blast radius *is* the repo takes the repo as a REQUIRED positional and joins the context skip set** — `repo rm`, `build purge`, and as policy `repo disable`. Commands whose blast radius is a *named child* (`secret rm NAME`, `cron rm NAME`, `template rm NAME`) may take the repo from context, because the target is still explicitly named.

**Skip set:** `{"context", "settings", "guide", "install", "auth", "server"}`. The group that reads/writes/explains sticky state must never be fed by it, or `context set` re-sets instead of erroring.

**`context show` reports provenance** — implicit state that changes results must be inspectable. Each key emits `{"value": …, "from": "DRONE_REPO"|"saved"|"git-remote"}` alongside `saved: [names…]` and `configPath`.

**Fix the gap OpenProject left.** `KNOWN_KEYS` there is defined once and imported **nowhere** — nothing validates it, so a renamed option makes a context key silently no-op. Here: drive `context set`'s options **from** `KNOWN_KEYS`, and add a unit test asserting every key matches ≥1 real option name in the tree. In Drone that test is also what catches the positional-vs-option mismatch at test time instead of via a sticky default that mysteriously does nothing. **Type coercion becomes live** (every OpenProject key was a string): validate at `context set` time, not at Click's converter, which would point the error at the *flag* rather than the *context*.

### 3.1a User requirements (2026-07-16) — these outrank everything below

Seven concrete asks. They sharpen the product; #1 in particular **supersedes** the
build-number-centric framing of `build wait` in §4.

1. **Address builds by COMMIT, not build number.** *"I just pushed, then Claude runs
   this to wait and see if this commit passed."* Build numbers are racy — two people
   pushing at once means "the latest build" is a coin flip, and an agent that waits on
   the wrong one reports someone else's failure. **Commit SHA is the only stable handle
   a caller already knows.** So: `drone-cli wait --commit <sha>` (and `--commit HEAD`,
   resolved from the local git checkout). Two hard parts, both real:
   - **The build may not exist yet.** A push → webhook → build has real latency. "No
     build for this commit" must mean *"not yet, still waiting"* for a grace period,
     then become a distinct, named failure — never silently "passed".
   - **One commit can have many builds** (push, then a restart, then a promote). Match
     on `after == sha`; pick by policy (`--event push` by default), and say which one
     you picked.
2. **Promote a build *or a commit*** with `--to <target>`, defaulting to **prod**, and
   the default itself settable (`settings set-promote-target`). Verified live:
   `POST /api/repos/{o}/{n}/builds/{n}/promote?target=prod` → 200, event `promote`.
3. **"Has this commit been promoted to prod?"** → builds with `event=promote`,
   `after == sha`, `deploy_to == target`, and their status.
4. **"Which commit is currently on prod?"** → newest **successful** `event=promote`
   build with `deploy_to == target` → its `after`, message, author, when. This is the
   question a CI UI cannot answer in one glance and an agent asks constantly.
5. **Commit links to the SCM** (Forgejo). Derive `{repo.link}/commit/{sha}` — verified
   live (200). But `repo.scm` is an **empty string** even on a synced repo, so the
   provider is *not* discoverable: default to the Gitea/Forgejo/GitHub pattern and let
   a setting override the base URL / pattern (GitLab and Bitbucket differ; both 404 on
   Forgejo — see §1.2 #4).
6. **Every setting has a sane default, or the CLI asks on first run.** No silent
   half-configured state.
7. **First run sets up login**: ask for the server URL, then — knowing the URL — *show
   where to get the token*: `https://drone.zierhut-it.de/account`. Derive that link
   from what the user just typed rather than making them hunt.

**Env vars (clarified):** `DRONE_SERVER` / `DRONE_TOKEN` are the ecosystem standard —
the official Go CLI uses them — and **must be supported**. `DRONECLI_*` are
higher-precedence aliases for everything we invent. The operator's stated preference is
the **keyring** for day-to-day use, with the env vars there for CI and compat.

> **Precedence hazard, called out deliberately.** The chassis contract is
> **env > keyring > file** — that is what makes a tool non-interactive in CI, and it
> must not be inverted. But it means an exported `DRONE_TOKEN` *silently overrides* a
> keyring login, and `DRONE_*` is also the namespace the Drone runner injects into every
> build step. So: `auth status` must always name the backend actually in use, and
> `server doctor` must say *"you are authenticating with $DRONE_TOKEN, not your keyring"*.
> Never make the operator guess which token spoke.

This forces the first real change to the shared chassis: `agentcli.Credentials` honours
exactly one env var (`<PREFIX>_TOKEN`). Supporting an ecosystem alias is a **general**
need (Drone → `DRONE_TOKEN`, Jira → `JIRA_API_TOKEN`, GitLab → `GITLAB_TOKEN`), so it
belongs in `AppSpec` as `token_env_aliases`, not in a Drone-local subclass. Tool #2
testing the seam is the point.

### 3.2 First run

Three TTY-gated, once-only, prompt-on-**stderr** hooks. Copy the discipline **verbatim** — it pays off harder here, because a `y/N` prompt inside a build step hangs the pipeline until its timeout.

1. **Default output format** — saved to `config.default_format` (tri-state: `None` = never chosen). Terminal default is **json** — the agent-first bet.
2. **Claude Code skill offer** — `claude_prompted` set **before** the install is attempted, so a decline, a crash, or a write failure all count as "asked". A nagging CLI is worse than a missing skill.
3. **Nothing else.** `auth login` is the only other interactive surface: server URL + pasted token, verified before persisting.

The gate is belt-and-braces: meta subcommands excluded, **`stdin.isatty()` AND `stdout.isatty()`** (stdin alone fires the prompt into `| jq` from a real terminal and hangs it forever), plus `CI != "true"` **and `DRONE != "true"`** — the runner injects the latter into every step, and a blocked prompt inside a pipeline hangs the build until its timeout rather than merely a shell.

Every failure path stays swallowed (`except Exception: pass`) — a first-run nicety must never fail a real command. Every prompt goes to **stderr** so stdout stays a clean channel.

---

## 4. Killer features — ranked

Ranked by value to an autonomous agent, which is the tool's primary caller. **The top tier is three:** `build wait`, `log failed`, and `usage report`.

---

**#1 — `build wait` / `--wait`: the async gate.**
Without this, an agent cannot use Drone at all. `POST /builds` returns `{status:"pending"}` and the API offers **no completion signal** — no webhook a CLI can subscribe to, no long-poll. Today an agent hand-rolls a poll loop and re-derives the terminal predicate.

* Driven by SSE `GET /api/stream` (real, undocumented, verified live) with a **poll fallback** for proxies that buffer SSE. `event: error / data: eof` is clean EOF.
* Terminal = `IsDone() || status == "blocked"` — the deliberate deviation from §2.4. **`blocked` is a distinct outcome**, not "still running": *"build 42 is blocked on stage 2 (deploy) awaiting approval — run `drone-cli build approve 42 --stage 2`"*. Get this wrong and an agent hangs until timeout on every gated pipeline.
* `--exit-code` opts into the 20–29 band. `drone-cli build run --wait --exit-code && ./deploy.sh` becomes a real shell primitive.
* `--timeout 30m` → exit 27, so a runner-less or stuck build is a bounded, *named* failure.

**#2 — `log failed` / `build debug`: the autonomous-debugging loop.**
This is what turns the CLI from "an API wrapper" into "an agent can fix its own CI".

Today: `GET build` → walk `stages[]` → find the failed step → know that stage/step are **1-indexed ordinals**, not names → `GET logs/{stage}/{step}` → decode the `{pos,out,time}` envelope → strip it. Two-plus calls and three pieces of tribal knowledge, and the payload is *unbounded prose*. An agent that fetches a whole build log burns thousands of tokens on `apt-get` noise and may not have context left to fix anything.

```bash
drone-cli log failed 42 --tail 80
drone-cli build debug 42     # one JSON doc: summary + first failing stage/step
                            # + tailed logs + durations + commit/branch/event
```

`--context 20` prints ±20 lines around the first error marker; `--errors-only` greps the conventional markers. The single biggest token-economy lever in the tool, and nothing upstream does it.

**#3 — `usage report`: the structural analogue of opcli's killer feature. NO RATES.**

**Decision (user, 2026-07-16): no rate table, no currency, no invoicing. Minutes only.** The command is therefore **`usage report`**, not `cost report` — calling it "cost" while it emits no money would be a lie in the command name, and the whole point of this feature is conspicuous honesty about what the number is.

That is a *narrowing of scope, not of ambition*. The generalized move is intact:

> When the API cannot give you the thing the business actually wants, don't give up and don't fake it. Find the primitive it *does* expose, aggregate client-side, and be conspicuously honest about the seams.

**The refused number here is the duration itself.** OpenProject refused *rates* (the hours were right there), so opcli supplied rates from local config. Drone refuses something more basic: **there is no duration field anywhere in the API** (§2.12) — not on builds, not on stages, not on steps. Only raw `created`/`started`/`finished` epochs. Nothing upstream will tell you how long a build took, how long it queued, or which step is eating your afternoon. So there is no missing dimension to import from a config file; the derivation *is* the whole feature, and it stands alone without rates.

```bash
drone-cli usage report --month 2026-07
drone-cli usage report --month 2026-07 --by-repo --detailed -o csv > july.csv
```

*Derivation.* Build wall time = `finished - started`; queue latency = `started - created`; per-step durations from the embedded `stages[].steps[]` (one `build info` = the whole tree). **Two bases, and the difference is real:** `--basis wall` (default) uses build wall time and needs **no fan-out** — `started`/`finished` are on the list response. `--basis stage` sums stage durations — *actual runner occupancy*, and the only basis that can break down by OS/arch (stages carry `os`/`arch`; builds don't). It costs one `GET .../builds/{n}` per build via the same bounded concurrent pool `build ls --stages` uses. For parallel stages `sum(stages) > wall`; for gated ones `wall > sum(stages)`. **State which basis produced the number in the output, never silently** — the two legitimately disagree, and a reader comparing two reports must be able to see why.

*Output.* Minutes and counts, per repo, rolled up by org (namespace): builds, total minutes, mean/p50/p95, queue time, failure rate. `--by-author` groups on `author_login`, labelled honestly: a build's minutes are *consumed by a repo*, not *worked by* a person — author is an attribution heuristic, not a labour record.

*Honesty rules, ported from the checklist and now the whole contract.* Emit `"minutes"`, never an `"amount"` key at all — an absent field is unambiguous where `"amount": null` invites a downstream agent to coerce it to `0` and quietly invoice work as free. **Accumulate unrounded; round once, at output** — rounding 200 builds before summing makes the total disagree with the sum of its own printed rows; an accountant notices and no test does. Builds still running have `finished == 0`: **exclude them and say how many you excluded**, never treat the epoch as a duration (that yields a 56-year build).

*Consequences of dropping rates:* delete `rates.example.json` from the layout (§6.2 / blueprint §2.2 tags it FRESH "if it has one" — Drone doesn't), delete `_rate_for` and its ordered-key determinism test from the Tier-1 list (§5.2), and drop `--rates`/`currency`/`billable` everywhere. **`duration.py` absorbs the minute math** and becomes the module this feature lives in. If rates are ever wanted later, `_rate_for` ports from opcli in an afternoon on top of this — the paging and derivation are the expensive parts, and they are built either way.

```python
result = {
    "period": {"month": month, "from": frm, "to": to},
    "basis": basis,                       # "wall" | "stage" — always stated, never implied
    "byRepo": by_repo, "byOrg": by_org,
    "totals": {"minutes": round(grand_minutes, 2), "builds": n_builds,
               "excluded": {"running": n_unfinished}},   # finished == 0; say so, don't hide it
}
# No "amount"/"currency"/"billable" keys at all. These are MINUTES. An absent field
# cannot be coerced to 0 by a downstream agent; a null one invites exactly that.
```

*The `--detailed` export is the second half and the sharper differentiator.* One row per build (`--detailed`) or per step (`--detailed --by-step`), with duration, queue latency, os/arch, event and author — **flat, ragged, and straight into a spreadsheet**. This is genuinely impossible in Drone's UI, which shows one build at a time and no duration column at all. It is also the only consumer that exercises the CSV renderer's hard-won rules: **header = union of keys across all rows, insertion-ordered** (not `rows[0].keys()` — a ragged row silently drops a column), and the CSV-specific cell coercer emitting `true/false` and embedded JSON so pandas parses it.

*Selection.* No server-side date filter exists, so page builds newest-first per repo and stop when `created < month_start` — a natural terminator that does **not** need `--max-pages` (unlike `--where status=failure`). The repo list comes from `GET /api/user/repos` in one call.

Put the reality check in the module docstring, exactly as opcli does — it is what stops a future maintainer "fixing" this by hunting for the duration endpoint that does not exist.

**#4 — `server doctor`: five failure modes, five distinct messages.**
Chain `GET /version` (reachable? which version?) → `GET /api/user` (token valid? admin? — the *only* valid token probe, since public repos read fine unauthenticated) → `GET /varz` (SCM budget, license seats) → a small capability probe. Today all of these collapse into `client error 401: {"message":"Unauthorized"}`.

Distinguishes: *server unreachable* / *URL isn't a Drone server* / *bad Drone token* / **Drone token fine but the SCM link is dead** (the 500-`{"message":"Unauthorized"}` case — the single most confusing failure in the whole system) / *not an admin* / *SCM quota exhausted* / *a 501 surface (oss build or Drone Cloud)*.

**#5 — `drone-cli status`: cross-repo build health in ONE request.**
The API has **no cross-repo build search** — the official CLI makes you loop. But `GET /api/user/repos?latest=true` (undocumented) returns every accessible repo *with its latest build attached*. One call → a fleet dashboard (repo / status / branch / event / derived duration / age), and the engine behind the `build failing` and `repo broken` presets.

**#6 — `secret audit`: answers what the API structurally cannot.**
Cross-reference `from_secret:` in the repo's `.drone.yml` against what exists server-side: **referenced-but-missing** (builds *will* fail, with a useless error); **defined-but-unreferenced** (dead credentials to rotate away); **exposed to pull requests** (`pull_request: true`); **`pull_request_push: true`** — the riskiest setting in the whole API and entirely undocumented. Because values are unreadable, the report is honest about what it can't diff.

**#7 — the cron seconds-first guard.** `"0 3 * * *"` fires **every hour**. Silent, invisible, 24× wrong, and the API cannot preview a schedule at all (`next` is only computed post-persist). See §2.7.

**#8 — silent-drop detection (repo PATCH, cron PATCH).** Two endpoints return **200 having changed nothing**: `repo update` drops `trusted/timeout/throttle/counter` for non-system-admins; `cron update` drops `name`/`expr` unconditionally and ignores malformed bodies entirely. The CLI diffs request vs response and fails loudly. No official client does this; it is the difference between an agent believing a change applied and knowing it didn't.

**#9 — `build restart --follow` / `cron trigger --follow`.** `restart` mints a **new** build number (verified live); today you restart and then guess which build is yours. `--follow` captures it and tails. `cron trigger` returns the created build object — which **drone-go discards** (`c.post(uri, nil, nil)`), so the official client literally throws away the number.

**#10 — `yaml verify` / `sign --check`: the endpoint that was promised and never shipped.** `POST .../verify` is a 404 (verified live) yet drone-go declares `pathVerify` and a `Verify()` method. Verification is reproducible client-side: POST the YAML *minus* its signature resources to `/sign` and compare the returned hmac against the file's `hmac:`. `sign --check` exits non-zero on a stale signature — a pre-commit hook instead of a build silently blocked ten minutes later.

*Honourable mentions:* `secret set` (idempotent upsert, killing the create-404-then-patch dance), `deploy status` (the undocumented `/builds/deployments` = what's live where), `template push ./dir` (replaces hand-JSON-escaping a template file into a `data` string), `orgsecret ls --all-orgs`, and flake detection (`restart` reuses the commit SHA, so **one commit with builds of differing status is a flake** — free, once the usage report's build-paging exists).

---

## 5. Test plan

### 5.0 The contributor promise — nobody installs Forgejo to send a patch

**This section leads the test plan deliberately.** Everything after it describes a Forgejo + Drone + Postgres + runner stack, and if that is the first thing a contributor reads, the honest reaction is "I'm not doing that" — and they're right. So state the contract first:

> **`pip install -e '.[test]' && pytest` must be green on a clean checkout with no Docker, no server, no tokens, no network.**

That is not aspirational; it is how the OpenProject CLI already behaves, verified today on a machine with nothing running and zero `OPCLI_*` env vars set:

```
$ pytest -m "not integration"     ->  144 passed, 103 deselected in 2.15s
$ pytest                          ->  144 passed, 103 skipped   in 2.10s
```

Two seconds, no infrastructure. **The heavy stack is for CI and for people touching the client↔server seam — never for a drive-by contributor.**

The mechanism ports verbatim from `conftest.py` and is three lines of real work:

```python
def _reachable() -> bool:
    if not BASE_URL or not TOKEN:      # not configured -> not live. No network call.
        return False
    try:    return _run(["auth", "whoami"]).code == 0   # configured but down -> also not live
    except Exception: return False

_LIVE = _reachable()                   # computed ONCE at collection, not per test

def pytest_collection_modifyitems(config, items):   # mark integration tests skipped when not live
```

Three properties make it work, and all three are load-bearing:

1. **Absent config is a skip, not a failure.** The default developer state is "no server", and the default state must be green. A red suite on a clean checkout trains people to ignore red.
2. **The skip reason is actionable**, not "skipped": `live OpenProject not configured (set OPCLI_BASE_URL + OPCLI_TOKEN)`. With `addopts = "-ra"` the skip summary is a *to-do list* for anyone who wants the deeper tier. Drone's equivalent names `DRONE_SERVER` + `DRONE_TOKEN` and points at `make up`.
3. **Configured-but-unreachable also skips**, via one real `auth whoami`. A half-booted stack must not produce 103 confusing failures.

**Two bugs in the OpenProject repo this exposes — fix them here, and fix them there:**

- **`make test-unit` is a lie.** Its help said "Run only the pure-unit tests (no live instance)" and it ran `pytest tests/test_unit.py` — **30 tests**. The hermetic set is **144**. *(Fixed in opcli 2026-07-16.)* A contributor following the Makefile gets 21% of the coverage and a green tick. Drone's Makefile: `test-unit` → `pytest -m "not integration"`, full stop. The marker is the source of truth; never a file list, which silently fails to grow when someone adds a file.
- **The README had no Contributing/Development section at all** — the capability existed and nothing advertised it; worse, its Testing section *opened* with `docker compose up`. *(Fixed in opcli 2026-07-16: it now leads with the two-line quickstart and demotes compose to "the deeper tier".)* Drone's README ships the same shape from day one.

**Consequence for the tiers below: Tier 1 must carry real weight.** Because Tiers 2/3 are heavy and therefore rare for contributors, the hermetic tier is what actually guards a PR. Target **~70:30 hermetic:live** (vs opcli's 58:42) — Drone makes this easy: no HAL, no lockVersion, no custom fields, and `--where`/presets/cron-parsing/duration-math/SSE-frame-parsing/`dronespec` are all pure functions over captured fixtures (§5.2).

### 5.1 The tiering decision, and why "mocks vs live" is the wrong framing

**Correcting a premise:** the OpenProject CLI does **not** have a MockTransport-based mock suite. Across **233 test functions**, `httpx.MockTransport` appears in **exactly one file** (`tests/test_client_retry.py`) and only where the transport itself is under test — retry policy, dry-run interception. It is **never** used to simulate API semantics. And `FakeClient` is used by exactly **two** test files (`test_wpfilters_unit.py`, `test_searchspec_unit.py`) plus its definition in `support.py` — the large unit files (`test_output_render.py`, `test_output.py`, `test_serialize_unit.py`, `test_unit.py`) use **no client collaborator at all**. They test pure functions over data. *(Counted, not recalled. Do not put a count in prose you have not counted — opcli's README claimed "56 tests" against a real 233, until it was fixed.)*

That line is principled and it is the answer here. The user's own `openproject-api-v3-gotchas.md` is a list of ~13 places where the obvious assumption about the API was **wrong**. Every one would have been encoded into a semantics mock incorrectly, and **the mock suite would have been 100% green while the CLI was broken against every real server.** Drone's gotcha list is *longer* — the docs are wrong about approve, decline's ACL, cancel's ACL, the logs curl example, the cron update body, secret response bodies, org-secret paths, and the `pref`/`prev` field name.

**So: do not build a semantics mock. Split on a different axis.** Drone has a property OpenProject doesn't — **the control plane and the execution plane are separable**, and the flakiness is almost entirely in the runner (docker.sock, an image pull per step, timing waits). The control plane is a Go binary and a database.

| Tier | Runs | Time | Scope |
|---|---|---|---|
| **1 — hermetic unit** | every PR, py3.10/3.11/3.12 matrix | seconds | no network |
| **2 — live control plane, NO runner** | **every PR** | ~60s | gitea + drone; ~80% of the surface |
| **3 — full stack + runner** | nightly + `workflow_dispatch` | ~5m | anything needing a build to actually execute |

Tier 2 is the key insight: a build created with no runner attached is a **real DB row with real JSON, real error codes and real auth behaviour** — it just sits `pending` forever. `POST /builds` → `Triggerer` → `sched.Schedule` merely enqueues; runners poll `/rpc/v2/stage` to claim work. So repo CRUD, secrets, crons, templates, users, build create/list/info/latest/restart/cancel, branches/pulls/deployments and sign/encrypt all get **full server fidelity without the flakiest component**. The bounded, honest loss: status transitions, logs, and cancel-a-*running*-build.

### 5.2 Tier 1 — unit

* **`test_client_retry.py` — MockTransport, ports near-verbatim.** The four pinned corners survive because the reasoning is protocol-level:
  * **429 retries any method incl. POST** (rejected, never processed → safe to replay).
  * **5xx retries only idempotent methods** — critically, **`POST /builds` must NOT be retried on 5xx**: a 502 may mean the build was queued and the response was lost; replaying **double-triggers CI**, which is more expensive and more visible than a double-created work package.
  * **404 never retried.** **`--dry-run` raises `DryRun` before any write leaves the process.** `Retry-After` as a **floor** (a server sending `Retry-After: 0` must not defeat backoff into a hot spin), 30s cap, jitter.
  * **Drop** the 409 arm. **Keep 500 out of `_TRANSIENT_STATUS`** — Drone's SCM-auth failure surfaces as 500 (§1.2) and retrying it just burns time. *(Note the body is JSON `{"message":…}`, not plain text — `render.InternalError` → `JSON(w, &errors.Error{...}, 500)`. Earlier drafts got the justification wrong; the conclusion stands.)*
  * **New:** a plain-text 401 (the `/metrics` shape) must still map cleanly; a wrong `/api/…` path returns a **plain-text `404 page not found`** and must map to exit 5, not explode in `json.loads`. Ordinary JSON-decode hygiene — not the SPA-HTML trap earlier drafts invented.
* **Pure logic, no client:** `_pop_globals` (both spellings, both positions, `--` sentinel); **the reserved-namespace assertion incl. `-V`**; cron parse + next-N-fire-times incl. the 5-field warning; epoch→ISO and duration math incl. `finished == 0`; **the usage report's accumulate-unrounded totals, the `finished == 0` exclusion, and `--basis` being stated in the output**; `_dotted_get`/`_project`; four renderers + three cell coercers incl. the CSV union header; secret redaction (incl. the `--fields data` denylist); **SSE frame parsing, with `event: error/data: eof` as normal EOF**; the pagination terminator against a >1-page fixture with the `per_page` clamp — pinning **both** `builds/list.go` (resets to 25) and `repos/all.go` (honours >100); log-body sniff (array vs JSONL); `_context_default_map` + the `KNOWN_KEYS`↔options test; **`dronespec` field names ↔ `SAMPLE_BUILD` keys**.
* **`support.py::FakeClient`** copies over and gets **simpler** (plain JSON, no HAL synthesis). `SAMPLE_BUILD/REPO/SECRET/CRON` are **real captured responses from the spike**. Keep the habit of annotating traps inline: the Drone equivalent of opcli's `# the admin collection link — must be ignored` is `build.stages[].steps[]` being `null` (not `[]`) on a pending build, which NPEs any naive step-count.
* With client-side predicates the tests get **stronger** than opcli's: assert *which builds a `--where` selects* against literal fixtures, not merely which dict it compiles to.

Expect roughly **70:30 unit:integration** (vs opcli's ~58:42) — no HAL, no lockVersion, no custom fields means less server-quirk surface that only a live instance can prove.

### 5.3 Tier 2/3 — the local stack: a PROVEN recipe, not a risk

**This is no longer a gate.** A live spike on 2026-07-16 booted the whole thing and drove a real pipeline to `success`, fully non-interactively — no browser, no OAuth click-through — then read its logs back. The steps below are what actually ran.

> **Reuse the in-house stack: `devops/ci-conversion-plugin/environment/`.** Zierhut IT already runs a working Forgejo + Drone + runner compose with `bootstrap_forgejo.sh` / `bootstrap_drone.sh`. **Start from that, not from the spike's throwaway compose.** What it teaches, and what we take from it:
>
> - **Forgejo, not Gitea** (`codeberg.org/forgejo/forgejo:9.0`). It is what we actually run in production, so it is the SCM the CLI must be right about. The Gitea API calls in the spike are unchanged — Forgejo is a Gitea fork and Drone drives it with the same `DRONE_GITEA_*` driver.
> - **`GET /api/healthz` → `{"status":"pass"}`** with real dependency checks (verified live) — a *genuine* readiness probe, and a much better gate than Drone's own `/healthz`, which is a liar (§1.2).
> - **The `socat` trick, and the bug class it fixes.** Drone talks to the SCM over the **public** URL, not the container name, so `DRONE_GITEA_SERVER=http://localhost:3001` must mean Forgejo *from inside Drone's container too*. `bootstrap_drone.sh` runs `socat TCP-LISTEN:3001,fork TCP:forgejo:3000 &` before `exec /bin/drone-server`, making the browser's URL and Drone's URL identical. This is the same bug class the spike hit from the other end: with `DRONE_SERVER_HOST=localhost:8080`, Forgejo registers a webhook pointing at *itself* and **no push ever triggers a build** (verified live — the push webhook silently never fired until `DRONE_SERVER_HOST` was repointed and `POST .../repair` re-registered the hook). **URL identity across containers is the single most confusing failure mode in this stack.** Whichever way you solve it, solve it once and comment it.
> - **Postgres + Redis, not sqlite.** This makes step 5 below a **one-line `psql -c "UPDATE …"`** with no `docker cp`/restart dance — the spike flagged that as attractive-but-unverified; the in-house compose already proves the Postgres path boots.
> - **`DRONE_CRON_DISABLED=true`** so no surprise cron build races the assertions; `DRONE_USER_CREATE=…,token:auth_token,admin:true` (note the **colon**, matching the spike's finding); `DRONE_COOKIE_SECRET` + `DRONE_COOKIE_TIMEOUT=720h`.
> - **It solves the SCM token the hard way, and it works**: a real OAuth app plus a full authorization-code flow driven by curl — logging into Forgejo, scraping `_csrf` out of the HTML, POSTing `/login/oauth/grant`, following the callback into Drone. See §5.3.1 for whether to keep that or use injection.

Compose: `codeberg.org/forgejo/forgejo:9.0` + `drone/drone:2.28.2` + `drone/drone-runner-docker:1.8.5` + `postgres` + `redis` (pin concrete tags; the in-house file floats `:latest` — don't). Tier 2 = `docker compose up -d`; Tier 3 = `docker compose --profile runner up -d`.

Five load-bearing steps, plus one seductive step (#3) you must **not** take:

1. **Forgejo admin user, via the CLI** (it runs as `git`, not root):
   `su git -- gitea admin user create --admin --username X --password Y --email Z -c /data/gitea/conf/app.ini` (the in-house script's exact form; the binary is still called `gitea` inside Forgejo).
2. **Forgejo PAT, via the API under basic auth**: `POST /api/v1/users/{user}/tokens {"name":…,"scopes":["all"]}` → read **`.sha1`**. Token *names* must be unique per user (the API hard-errors on reuse) → the in-house script timestamps them (`token_$(date +%Y%m%d%H%M%S)`) for re-seed idempotency. Copy that.
3. **The OAuth app is optional — and which way you go decides step 5.** See **§5.3.1**. If you inject (recommended for CI), `DRONE_GITEA_CLIENT_ID`/`_SECRET` are literal dummy strings — **the spike ran that way end-to-end and everything worked**; Drone only needs them present to *boot*. Note that a **real** `ClientID` activates Drone's token-refresh path (`provideRefresher` only builds a refresher `case config.Gitea.ClientID != ""` **[src]**), which an injected PAT has no refresh token to satisfy — so dummies are not a shortcut, they are the *safer* path when injecting.
4. **`DRONE_USER_CREATE=username:droneadmin,machine:false,admin:true,token:<KNOWN_TOKEN>`** → seeds a Drone user with a **known API token**, so no OAuth is needed for API auth. **Proven** (and the in-house stack does the same with `token:auth_token`). Note the colon: `config.go` does `parts := strings.Split(param, ":"); if len(parts) != 2 { continue }`, so `token=…` is **silently skipped** and Drone mints a random token instead. Values compare against the exact string `"true"` — `admin:True` is false.
5. ***** **The key step — give the user an SCM token.** The Drone API token is **not** the SCM token. Anything touching the SCM (sync/activate/repo list) reads `users.user_oauth_token`, which no API can set. Without it, `POST /api/user/repos` returns **500 `{"message":"Unauthorized"}`** (verified live). Two ways to satisfy it — §5.3.1.
6. **`DRONE_RUNNER_NETWORKS=<compose network name>`** on the runner — **required**, or the clone step dies with `fatal: unable to access 'http://forgejo:3000/…': Could not resolve host`. This actually happened; build containers otherwise land on the default bridge.

Then the happy path, all verified green: `POST /api/user/repos` (sync) → repo appears → `POST /api/repos/{o}/{n}` (activate) → `POST /api/repos/{o}/{n}/builds?branch=main` → runner executes → logs readable.

#### 5.3.1 The SCM token: inject, or run the real OAuth flow?

Both are proven. They trade different kinds of brittleness, and the choice is worth making deliberately.

| | **A — Inject the PAT** (spike) | **B — Real OAuth flow** (in-house `bootstrap_forgejo.sh`) |
|---|---|---|
| How | `UPDATE users SET user_oauth_token='<PAT>'` | Create an OAuth app, then curl the authorization-code flow: login → scrape `_csrf` → POST `/login/oauth/grant` → follow the callback |
| Size | ~1 SQL statement | ~45 lines of shell |
| Brittle against | Drone's **private DB schema** (`users.user_oauth_token`) and encryption being off | Forgejo's **login/grant HTML** (`awk -F 'value="'` over markup), plus `sed`-rewriting ports between container and host |
| Needs `socat` | No — use the container name for `DRONE_GITEA_SERVER` | Yes — the browser's URL and Drone's URL must be identical |
| Drone **UI login** works | **No** | **Yes** |
| Token realism | A PAT masquerading as an OAuth token; no refresh token | Genuine, refreshable |

**Recommendation: A for CI, B available as a `dev` profile.** A CI seed must stay green unattended for years, and *scraping a CSRF token out of Forgejo's login page is a UI-version dependency in a test harness* — Forgejo 9 → 10 can break it with no API change. The DB write depends on a schema that has been stable across Drone 2.x and is trivially assertable (`rowcount == 1`, else `DRONE_USER_CREATE` didn't parse). But B is what makes the Drone **web UI** usable locally, which is worth real money when debugging — and it already exists and works, so keep it as an opt-in profile rather than deleting it.

With **Postgres** (per the in-house compose) A is a one-liner and the sqlite `docker cp` dance disappears entirely:

```sh
# A, on Postgres — no cp, no restart
psql "$DSN" -c "UPDATE users SET user_oauth_token='$PAT' WHERE user_login='droneadmin'"
```

On sqlite it is clumsier — the Drone image is bare and has **no `sqlite3` binary**, so you patch on the host (`docker cp` out → python3 `sqlite3` → `docker cp` back → restart). Either way it works **only** because `DRONE_DATABASE_SECRET` / `DRONE_DATABASE_ENCRYPT_USER_TABLE` are unset → `encrypt.New("")` → `&none{}` passthrough **[src]**. Assert both are unset in the seed, loudly — if someone sets a database secret later, the injected token becomes ciphertext-garbage and the failure surfaces as that same baffling 500.

Other settings that earn their comment: `GITEA__security__INSTALL_LOCK=true` (skip the web wizard); `GITEA__server__ROOT_URL=http://gitea:3000/` (drives the clone_url the runner resolves); `GITEA__security__ALLOWED_HOST_LIST=*` **and** `GITEA__webhook__ALLOWED_HOST_LIST=*` (the default `external` blocks webhook delivery to Drone's private container IP — symptom: enable succeeds, no build ever fires); `DRONE_DATADOG_ENABLED=false` (the image Dockerfile defaults it **true** and phones home); matching `DRONE_RPC_SECRET` on both sides; `auto_init=true` on the demo repo (an empty repo has no commit to build).

**Readiness: don't gate on `/healthz`.** It answers 200 **[live]**, but it answers 200 *early* — it is not a proof that the DB migrated or that the `DRONE_USER_CREATE` bootstrap ran *(that it performs no DB check is read from source, **[src]**, not observed)*. Gate on `GET /api/user` with the known Bearer token instead — it proves server-up **and** bootstrap-ran **and** the token is valid, in one probe. Direct analogue of opcli's `conftest.py::_reachable()` shelling `auth whoami`. The seed ends in a `SEED_OK` sentinel that a `grep -E '^SEED_OK'` + `set -euo pipefail` turns into a hard CI failure.

**`make up && make env && make test`** mirrors opcli's Makefile — but note the blueprint mislabels that file **VERBATIM**; it is **PARAMETERIZED**. Its `env`, `token` and `seed` targets are entirely OpenProject-specific (`env` shells `get_admin_token.sh` twice and writes `OPCLI_SECOND_TOKEN`). Here `env` is trivial: the token is a compose constant, so `get_admin_token.sh` and its 10× retry loop are **deleted**.

**A second, non-admin actor is a decision, not a question.** `acl/org.go:49` is a literal unconditional `next.ServeHTTP` for system admins, so **admin-token tests exercise essentially no authorization logic** — and `DRONE_SERVER_PRIVATE_MODE` being unset means the public demo repo doesn't 403 either (§2.1). Drone's ACLs are precisely what its docs are most wrong about (approve, decline, cancel, cron-list). So: seed a second Gitea user + PAT + Drone user, wire `DRONECLI_SECOND_TOKEN` through the Makefile's `env` target, `ci.yml`, and `conftest.py` (`pytest.skip` when unset), and use opcli's existing `_run(..., token=…)` per-invocation override. That mechanism already exists; only the env var name is new.

**`conftest.py`** copies verbatim otherwise: `pytest_collection_modifyitems` skips `integration` when unreachable, `_run` → `[sys.executable, "-m", "dronecli", "-o", "json", …]` + `Result(code, stdout, stderr).ok().json`, `DRONECLI_CONFIG_DIR` isolation, and the `pty.fork()` first-run test (pre-seed `default_format` or the format prompt eats the `y\n`; catch `OSError` — a Linux pty raises EIO on child exit).

**One new marker: `needs_runner`** (Tier 3). PR CI runs `-m "integration and not needs_runner"`. `timeout = 120` stays global; build-executing tests get `@pytest.mark.timeout(600)`. *(No `needs_nonoss` — see §2.0.)*

**Teardown:** builds **cannot be deleted**. Session fixture = "ensure a scratch repo exists and is activated"; per-test fixture yields a triggered **build number** with no teardown. Assert on *the number you triggered*, never "the latest build", or parallel tests interfere. Secrets and crons delete cleanly → keep the opcli fixture shape.

**Keep the opcli skip discipline:** anything the *server version/build* controls degrades to a **narrow** `pytest.skip` matching a specific signal; anything the *CLI* controls is a hard assert. Never a blanket try/except — real regressions hide inside skips. `addopts = "-ra"` makes the skip summary the version-skew report.

**No compat matrix.** Drone is frozen; a version axis yields almost no signal. The high-value axis, if one is ever wanted, is the **SCM provider** (gitea vs github) — that's what changes payload shapes and auth flows.

### 5.4 What honestly stays mock-only or untested

| Surface | Why | Cover |
|---|---|---|
| Retry policy (429/5xx/Retry-After) | You cannot make a server 429 on demand | MockTransport, Tier 1 |
| The 501 arm (exit 8) | `drone/drone:2` never 501s (§2.0) | MockTransport fixture only |
| Drone Cloud behaviour | No instance | Untested. Say so in the README. |
| `--exit-code` band 20–29 for `error`/`declined`/`skipped`/`waiting_on_dependencies` | Not reproducible on demand in the test stack | Unit test over the predicate; the enum values come from source, not observation |
| 402 (repo/seat limit) | Reachable — the published image is the license-limited build | Tier 2, if we choose to push past the limit |

### 5.5 Remaining risks (the big one is gone)

| Risk | Severity | Mitigation |
|---|---|---|
| The DB injection is an **unsupported private-schema dependency** (column name, plaintext-by-default) | Medium | Pin the image exactly. Assert **behaviourally** (sync returns repos), never "the UPDATE succeeded". One line, in the seed only, never in the CLI. |
| `DRONE_DATABASE_SECRET` / `ENCRYPT_USER_TABLE` "hardening" silently breaks the injection | Medium | Loud comment; the seed asserts both unset. |
| The **1-hour perm-staleness time bomb**: `acl/repo.go` re-syncs stale perms and does `render.NotFound` **on error, aborting before the admin bypass** — a suite running >1h, or a broken PAT, turns green into mystery 404s | Medium | Machine users never sync → no perm rows → dormant. Don't let tests depend on it. Gitea PATs don't expire. |
| Runner: docker.sock (root-equiv), per-step image pulls, egress, leaked containers | Medium | Tier 3 only, nightly. Pre-pull `drone/git` + `alpine`. |
| Gitea `INSTALL_LOCK` via env silently no-ops against an existing `app.ini` (go-gitea #25924, #26992); builds sit `pending` forever with no runner | Low | Treat the gitea volume as disposable, `down -v` to re-seed; `needs_runner` marker + `timeout = 120`. |
| **Strategic: Drone is in maintenance** | — | Images still ship. Viable, not abandoned — and a frozen API is *good* for a wrapper. But it's a bet (§8). |

---

## 6. Naming decisions

### 6.1 The command name — **`drone-cli`** (DECIDED)

**Decision: `drone-cli`.** Chosen by the user, 2026-07-16.

The official Go CLI already owns `drone` on PATH, so that name is refused — the same call opcli made with `op`, and for the same reason: a package manager should never win a PATH fight it didn't announce, and here the collision would be with *the official client for the very tool we wrap*.

`drone-cli` is **collision-free**: the upstream project is *named* `drone-cli` (github.com/harness/drone-cli), but the binary it installs is `drone`. Nothing occupies `drone-cli` on PATH.

| Option | Verdict |
|---|---|
| `drone` | **NO.** Shadows the official CLI, which every Drone user has. |
| `drone-agent` | **NO.** "Agent" *is* a Drone concept — the runner was literally called the agent. Actively confusing. |
| `dcli` / `dr` / `dq` | **NO.** Cryptic, and short names are exactly the collision class we're avoiding. |
| `dronectl` | Considered and passed over. |
| **`drone-cli`** | **CHOSEN.** No PATH collision. Reads as exactly what it is, and matches the dist name `agent-tool-drone-cli` and the config dir `~/.config/drone-cli/` — one word throughout the product. |

The one wart to own: the upstream *project* shares this name while shipping a differently-named binary, so "drone-cli" is briefly ambiguous in prose. Cheap: the README and `guide` both open by stating that `drone-cli` is this tool and `drone` is the official Go client, and that **both can be installed side by side** — which is the point of not claiming `drone`.

```toml
[project.scripts]
# Only `drone-cli`. The name `drone` is intentionally NOT claimed — it belongs to the
# official drone/drone-cli binary, which most Drone users already have on PATH; both
# are designed to coexist. `drone-agent` is also refused: "agent" means the runner in
# Drone's own vocabulary. Add your own alias if you want it shorter, e.g. `alias dc=drone-cli`.
drone-cli = "dronecli.cli:main"
```

### 6.2 The rest

| Thing | Decision | Rationale |
|---|---|---|
| **PyPI distribution** | `agent-tool-drone-cli` | Matches the convention, and the command name now matches the dist name's tail — one word (`drone-cli`) across command, dist, config dir and keyring service. Description still ends "Installs the `drone-cli` command." so the metadata is explicit. |
| **Import package** | `dronecli` (`src/dronecli/`) | Short, private, mirrors `opcli`. |
| **Env prefix** | `DRONE_SERVER`/`DRONE_TOKEN` (read — ecosystem standard) · `DRONECLI_SERVER`/`DRONECLI_TOKEN` (higher-precedence aliases) · **`DRONECLI_*` for everything invented** | Adopting the ecosystem's auth names is worth more than prefix purity. But a Drone CLI runs **inside Drone**, where `DRONE_*` is the runner's injected namespace — so `DRONECLI_CONFIG_DIR`, never `DRONE_CONFIG_DIR`. |
| **Config dir / keyring** | `DRONECLI_CONFIG_DIR` > `XDG_CONFIG_HOME` > `~/.config`, then `/drone-cli/`; keyring service `drone-cli` | The official CLI has no config dir (pure env), so this is free. **`config_dir()` must be a FUNCTION, not an import-time constant** — that single property is the entire basis of hermetic tests. |
| **Vocabulary** | `--server/-s`, not `--base-url` | Mirror Drone's own words even though `Profile.base_url` stays internally. |
| **Claude skill** | name `drone-ci`, dir `~/.claude/skills/drone-ci/` | The frontmatter `description` is the **entire matching surface** — but "build", "CI", "pipeline", "deploy" over-fire on every unrelated build question. **Anchor every trigger to the product noun**: "Drone CI", "a Drone build", "Drone secrets", "restarting a Drone build". Narrow the vocabulary, don't widen it. |
| **Version** | `dynamic = ["version"]` + `[tool.setuptools.dynamic] version = {attr = "dronecli.__version__"}` | Do **not** copy opcli's two-file duplication. Its `--version` test only proves the CLI reports what `__init__.py` says; it cannot catch bumping `pyproject.toml` and forgetting `__init__.py` — shipping 0.5.0 that reports 0.4.0 in its UA and in the skill it writes to users' machines. |
| **Module naming** | DI container → **`appctx.py`** (`AppContext`) | So `commands/context.py` unambiguously means the sticky-defaults feature. opcli has three colliding meanings of "context" one directory apart, forcing `from .commands import context as context_cmd`. Cheap now, annoying later. |

Two notes on the blueprint's layout tree as it applies here: **`hal.py`** (the wire-format adapter seam) is **deleted** — Drone is flat JSON, and this is the single largest deletion in the port — but per the blueprint's own rule the *seam* survives the deletion, so the wire format keeps exactly one home and `output.py` never learns it. **`duration.py`** survives and grows: it is the natural home for epoch→ISO, `duration_seconds`, and the usage report's minute math. `rates.example.json` is **not** ported — there are no rates.

---

## 7. Phased build plan

| Phase | Deliverable | Size |
|---|---|---|
| **0 — Spike** | **Largely DONE** (2026-07-16). The stack boots, a pipeline ran green, the endpoint map is captured. Remaining: commit `docker-compose.yml` + `scripts/seed_test_data.sh` from the spike's transcript with concrete pins; capture real JSON per resource into `tests/support.py`; **capture the `GET /api/stream` frame payload** — `build wait` and the whole 20–29 band rest on whether a frame carries enough to evaluate the terminal predicate without a follow-up GET, and that is the one thing the spike observed but did not pin. | **0.5d** |
| **1 — Chassis** | Scaffold + `pyproject` (dynamic version) + Makefile (parameterized). `cli.py` (`_pop_globals` incl. `--profile`/`--no-color`/`-V`, `_ERROR_FORMAT`, central handler, `DryRun` catch, **`pretty_exceptions_show_locals=False` — a security decision: locals hold the token**). `paths.py`, `appctx.py`, `config.py`, `credentials.py` (verbatim), `errors.py` (+ exit 8), `output.py` (verbatim + `stream_lines` + the Rich-markup guard + **`message()` as an allowlist `== table`, not opcli's denylist `!= json`, which corrupts csv/markdown**), `client.py` (bearer, `/api` root, retry matrix, `per_page` clamp + short-page terminator, `DryRun`, `{"message"}` mapping, `collect_cached` from day one). Groups: `auth`, `settings`, `context`, `guide` (skeleton), `install`, `raw`, `server`. **The reserved-namespace test.** Tier 1 suite. | **2–3d** |
| **2 — Read surface** | `serialize.py` (epoch→ISO, derived `duration_seconds`, secret redaction) + `duration.py`. `repo ls/info`, `build ls/info/last/branches/pulls/deployments/feed`, `log view`. `_COLUMNS` per module. Tier 2 harness + `conftest.py` + seed script + second-actor token. | **2d** |
| **3 — THE AGENT LOOP** (early; it's the product) | `build wait/watch` (SSE + poll fallback, the `IsDone() \|\| blocked` predicate, the exit-code band). `log follow` (REST backfill + SSE, **dedupe on `pos` — the overlap is guaranteed**, `eof`-is-normal). **`log failed`**, **`build debug`**, `--tail/--grep/--context`. `build restart --follow`. Tier 3 + `needs_runner`. | **2–3d** |
| **4 — Write surface** | `repo enable/disable/rm/sync/chown/repair/update` (+ **silent-drop diff**, visibility validation, `--trusted` gate, `--counter --unsafe`) + presets. `build run/restart/cancel/promote/rollback/approve/decline/purge` (+ the `custom`-event warning, the param-precedence-inversion warning, `--to` preflight). `secret` CRUD + **`set` upsert** + redaction + `--from-*`. `orgsecret` (**`/api/secrets/{ns}`**). `cron` CRUD + **the seconds-first guard** + `next` + update emulation. `template` CRUD + `push`/`pull`. **`--dry-run` on every write from day one** — Drone's writes ship code. | **2–3d** |
| **5 — Killer analytics + discovery** | **`usage report`** (+ `--basis wall|stage`, `--by-repo`/`--by-author`, `--detailed [--by-step] -o csv`) — the flagship. **No rates** (decided). `dronespec.py` + **`fields`/`operators`/`values`** + the two registry guard tests. `--where` predicate engine + `--max-pages` + the truncation signal. The four presets. `drone-cli status`, **`secret audit`**, `deploy status`, **`server doctor`**, flake detection. | **2–3d** |
| **6 — YAML tools** | `yaml lint` (client-side, honest "2/5 linted"), `sign` (+ `--check`, Starlark/Jsonnet refusal), **`verify`** (synthesized), `yaml secret add` (encrypt→splice→re-sign), `explain`. | **1d** |
| **7 — Admin** | `user` CRUD (+ machine-token capture warning, block blast-radius, rotate-token WART), `queue pause/resume`, `server stats`, `repo collab`, `build running`. | **1d** |
| **8 — Ship** | `guide` OVERVIEW + all topics (output contract **with the logs carve-out**, exit table, `DRONE_SERVER`/`DRONE_TOKEN` first, gotchas incl. the no-Drone-counterpart list). `AGENTS.md` (same fixed section order the README gets), README (**Known limitations (API, not the CLI)**), `SKILL.md` + `install claude`, `scripts/gen_docs.py` **wired into CI with `git diff --exit-code docs/`** (the gap opcli left — its docs-never-drift claim is currently unenforced). `ci.yml` (3 jobs), `nightly.yml` (Tier 3), `release.yml` (4-way OS matrix + PyPI Trusted Publishing). PyInstaller with keyring `--collect-all` + per-OS backends — **move the flag list into a `.spec`/`build_binary.py`** so the Windows PowerShell job can't drift from the bash script. | **1–2d** |

**Total ≈ 11–15 days** (slightly down: no rate table, no `_rate_for`, no `rates.example.json`). Phases 1–3 are the minimum shippable agent tool.

---

## 8. Open questions

Everything the spike settled has been removed from this list — the testbed, `DRONE_USER_CREATE`, the SCM-token injection, `DRONE_RUNNER_NETWORKS`, the OAuth-app question, and the entire oss/501 story are all **decided and proven**.

1. **Is Drone the right target at all?** `drone/drone` and `harness/drone` both redirect to `harness/harness`; the Drone v2 tree survives only on the `drone` branch / `v2.x` tags; `main` is the unrelated Harness Open Source, which Harness calls "the next major version of Drone". Images still ship (2.28.2, runner 1.8.5), so this is **viable, not abandoned** — and a frozen API is actually *good* for a wrapper. But it's a deliberate bet. **Should the target be Harness Open Source instead, or is a stable, frozen Drone exactly the point?**

2. **Which Drone are we actually driving?** Self-hosted, cloud.drone.io, or both? This decides whether **org secrets** exist at all (disabled on Drone Cloud), whether the admin groups (§2.9/§2.10) are reachable or dead weight, and whether exit 8 is theoretical or routine. Is there a real instance to point the CLI at during development?

3. **`cost report`: is the billing model right?** This is the one place your domain knowledge beats mine. (a) The natural axis is **repo/org**, not person — Drone has no labour record, only build authorship. Is repo-level invoicing what you'd actually bill a client, or do you need `--by-author` to be first-class? (b) Rates are per **build-minute** with `os_arch` as the secondary key — is that your cost driver, or is it flat-per-repo? (c) Default `--basis wall` (cheap, one page-walk) vs `--basis stage` (accurate runner occupancy, one extra GET per build): which should be the default, given a month of a busy fleet could be thousands of requests? (d) Do you want the `--detailed` CSV keyed one-row-per-**build** or per-**step** by default?

4. **`drone-cli` — approved?** And is the `drone`-shadowing refusal right, or do you want `drone` claimed because you don't use the official CLI? (Also: is `dronecli` free as a top-level import in your environments? The dist is `agent-tool-drone-cli`, but a colliding top-level package in `site-packages` is still a hazard.)

5. **`--exit-code` band (20–29) vs the "never leak status into the exit code" rule.** I've made it strictly opt-in with a reserved band. Alternative: no band — `--exit-code` maps success→0, everything else→1. Preference?

6. **Runner in PR CI, or nightly only?** I've recommended nightly (docker.sock + per-step image pulls + timing waits). That means `build wait` / `log follow` / `log failed` — the top-tier features — are only proven nightly. Accept, or pay the flake tax on every PR? The spike showed the runner is reliable when the network is configured; the open part is CI-runner cost and flake tolerance, not feasibility.

7. **Which killer feature ships first if we have to cut?** My order: `build wait` → `log failed` → `cost report`. If invoicing is the commercial driver, that inverts to `cost report` first — but `cost report` needs the build-paging that Phase 2 builds anyway, so the reorder is cheap. Confirm.

8. **A `ghcr.io` image in the release matrix — in scope for v0.1?** It's the natural distribution channel for a CI CLI (`image: ghcr.io/…/drone-cli` as a pipeline step) and has no OpenProject precedent. ~30 lines of `release.yml`, but new surface.
