# Drone API — findings VERIFIED against a live drone/drone:2 + gitea 1.22 + runner stack
(Empirical, from an actual spike on 2026-07-16. Trust these over the docs.)

## Testbed: PROVEN to work end-to-end, fully non-interactive
Ran a real pipeline to `success` and read its logs. No browser, no OAuth click-through.

Compose = `gitea:1.22` + `drone/drone:2` + `drone/drone-runner-docker:1`.

Seeding sequence that works:
1. `docker exec gitea gitea admin user create --username X --password Y --email Z --admin --must-change-password=false`
2. Gitea PAT via API basic-auth: `POST /api/v1/users/{user}/tokens {"name":..,"scopes":["write:repository","write:user","read:organization"]}` -> `.sha1`
3. **NO OAuth app registration is needed.** `DRONE_GITEA_CLIENT_ID`/`_SECRET` can be literal dummy strings
   (the spike ran with `"dummy-client-id"`/`"dummy-client-secret"` throughout and everything worked).
   Drone only needs them to *boot*; they are used solely for the browser login flow, which we bypass.
   (Gitea CAN mint an oauth app non-interactively via `POST /api/v1/user/applications/oauth2` — verified —
   but it is dead weight for the test stack. Don't do it.)
4. `DRONE_USER_CREATE=username:droneadmin,admin:true,token:<KNOWN_TOKEN>` -> seeds a drone user with a KNOWN API token. **This is the key: no OAuth needed for API auth.**
5. **Critical**: the drone token != the SCM token. Anything touching the SCM (sync/activate/repo list) needs `users.user_oauth_token` populated. Inject the Gitea PAT:
   `docker cp drone:/data/database.sqlite . && sqlite: UPDATE users SET user_oauth_token='<gitea PAT sha1>' WHERE user_login='droneadmin' && docker cp back && restart`
   (drone image has NO sqlite3 binary -> patch on the host with python3's sqlite3.)
6. `DRONE_RUNNER_NETWORKS=<compose_network>` on the runner, else the clone step fails with
   `fatal: unable to access 'http://gitea:3000/...': Could not resolve host: gitea`
   (build containers otherwise land on the default bridge.)

Then: `POST /api/user/repos` (sync) -> repo appears -> `POST /api/repos/{o}/{n}` (activate) ->
`POST /api/repos/{o}/{n}/builds?branch=main` -> runner executes -> logs readable. VERIFIED green.

Without the oauth-token injection, `POST /api/user/repos` returns **HTTP 500 with body `{"message":"Unauthorized"}`**
(drone maps SCM auth failures to 500 — bad mapping; the CLI must not treat 500 as purely transient/retryable).

## Endpoint map (status codes observed live)

### meta / unauthenticated
| endpoint | result |
| --- | --- |
| `GET /api/version` | **404 — DOES NOT EXIST** (docs imply otherwise) |
| `GET /version` | 200 — the real version endpoint, unauthenticated |
| `GET /healthz` | 200 — use as the compose healthcheck |
| `GET /varz` | 200 (admin/debug info) |
| `GET /metrics` | 401 unless `DRONE_PROMETHEUS_ANONYMOUS_ACCESS=true` |

### user / admin
- `GET /api/user` 200 -> `{id,login,email,machine,admin,active,avatar,syncing,synced,created,updated,last_login}`
- `GET /api/user/repos` 200 · `POST /api/user/repos` = **sync** (needs SCM token)
- `GET /api/user/builds` 200 — **returns REPO objects (latest build per repo), not builds.** It's a feed.
- `GET /api/users` 200 (admin) · `POST /api/users` 200 (create; `{"login":..,"machine":true,"admin":false}`)
- `GET /api/repos` 200 (admin, all repos)
- `GET /api/queue` 200 · `GET /api/nodes` **404** (nodes is a DB table but no OSS route)

### repos
- `GET|PATCH|DELETE /api/repos/{owner}/{name}` — PATCH 200 (`{"timeout":90,"protected":true}`)
- `POST /api/repos/{owner}/{name}` — activate 200
- `POST .../repair` 200 · `POST .../chown` 200
- Addressing is `{namespace}/{name}` (slug). 404 before activation.

### builds
- `GET /api/repos/{o}/{n}/builds` — list; `GET .../builds/{number}` — **embeds `stages[].steps[]`, one GET = whole tree**
- `POST .../builds?branch=main` — trigger; event = `custom`
- `POST .../builds/{n}` — **restart: creates a NEW build number** (restarting #1 produced #2). Big CLI semantic.
- `DELETE .../builds/{n}` — cancel 200
- `POST .../builds/{n}/promote?target=prod` 200 — event = `promote`
- `POST .../builds/{n}/approve/{stage}` / `decline/{stage}` — exist (400 when build isn't blocked)
- statuses seen: pending, running, success, failure, killed; events: push, custom, cron, promote

### logs
- `GET /api/repos/{o}/{n}/builds/{b}/logs/{stage}/{step}` -> JSON array of `{pos,out,time}`
- `DELETE` same path -> 204 (purge); subsequent GET -> 404
- Never-ran step -> 404 `{"message":"sql: no rows in result set"}`

### secrets  (data is WRITE-ONLY — never returned; CLI can list/rotate names, not read values)
- repo: `GET|POST /api/repos/{o}/{n}/secrets`, `GET|PATCH|DELETE .../secrets/{name}` — all 200
- org: **`/api/secrets/{namespace}`** — POST/GET 200. NOT `/api/orgs/{ns}/secrets` (404, even with a real org).

### cron
- `GET|POST /api/repos/{o}/{n}/cron`, `POST .../cron/{name}` = **execute now** -> creates a build with event `cron`
- object: `{id,repo_id,name,expr,next,prev,event,branch,disabled,created,updated,version}`

### templates  (namespaced — unlike secrets, `data` IS returned)
- `POST|GET /api/templates/{namespace}` 200. `POST /api/templates` -> **405**.

### yaml tools
- `POST .../sign` 400 (exists, needs proper payload) · `POST .../encrypt` 400 (exists)
- `POST .../lint` **404** · `POST .../verify` **404** — DO NOT ship these; they are not in drone 2 OSS.

### SSE STREAMING — real, and undocumented. The basis of the killer feature.
- `GET /api/stream` -> `text/event-stream`; emits `: ping` then `data: {repo json}` on build state changes (global feed).
- `GET /api/stream/{owner}/{repo}/{build}/{stage}/{step}` -> live log tail; emits `: ping`, log lines, then `event: error / data: eof` at end.
- Note `curl -I` on it returns 405 (HEAD unsupported) — must GET.

## Implications for the CLI
- No HAL, no lockVersion, no optimistic locking -> `update_locked()` chassis code is dropped entirely.
- Repos are addressed by `owner/name`, not numeric id -> the resolver becomes a slug parser + `context` default owner.
- Builds are async & long-running -> `--wait`/`--follow` with proper exit codes is the defining feature (OpenProject had no analogue).
- Secrets being write-only kills any "export secrets" idea; a **drift audit** (which repos lack secret X) is still possible from names.
- 500-with-"Unauthorized" means retry logic must NOT blindly retry 500s.

---

## Round 2 (same day): SCM links, webhooks, Forgejo

### `build.link` is NOT a commit link — verified live, both shapes
| event | `build.link` |
| --- | --- |
| `push` | `http://gitea:3000/droneadmin/linktest/compare/<before>...<after>` — a **compare** page |
| `custom` | `http://gitea:3000/api/v1/repos/droneadmin/linktest/git/commits/<sha>` — an **API** URL |

Drone passes through whatever the SCM gave it (webhook payload vs API response) and never normalizes.

**The derivation that works** (200 on Forgejo/Gitea): `{repo.link}/commit/{build.after}`
- `repo.link` = `http://gitea:3000/droneadmin/linktest` (the web URL) · `git_http_url`, `git_ssh_url` also present
- URL-pattern probes on Forgejo: `/commit/<sha>` -> **200** · `/commits/<sha>` -> 303 · `/-/commit/<sha>` -> 404
- `/src/branch/main` -> 200 (Forgejo/Gitea branch URL)
- **`repo.scm` is an EMPTY STRING** even on a synced+enabled repo -> the SCM type is NOT discoverable from the object.

### Webhook delivery: the URL-identity trap (verified live)
With `DRONE_SERVER_HOST=localhost:8080`, Forgejo registers the webhook as `http://localhost:8080/hook?secret=…`
— which from inside Forgejo's container is Forgejo itself. **Push events silently never fire a build.** No error anywhere.
Fix: point `DRONE_SERVER_HOST` at something both sides resolve (e.g. the container name), then
`POST /api/repos/{o}/{n}/repair` to re-register. Note repair **appends** a second hook rather than replacing the stale one.
Same bug class, other direction: the in-house stack runs `socat TCP-LISTEN:3001,fork TCP:forgejo:3000` inside
drone so `DRONE_GITEA_SERVER=http://localhost:3001` resolves for BOTH the browser and drone.
=> Triggering builds via the API (not webhooks) keeps the seed independent of this entirely.

### In-house prior art: devops/ci-conversion-plugin/environment/
Forgejo 9 + Drone + runner + postgres + redis, with `bootstrap_forgejo.sh` / `bootstrap_drone.sh`.
- Forgejo `GET /api/healthz` -> `{"status":"pass"}` + per-dependency checks (verified live) — a real readiness probe.
- Solves the SCM token the HARD way: real OAuth app + curl-driven authorization-code flow (scrapes `_csrf` from HTML).
  Works, gives a genuine refreshable token, and makes the Drone **UI login** usable — at the cost of a
  UI-version dependency inside the test harness.
- Uses `DRONE_USER_CREATE=…,token:auth_token,admin:true` — independently confirms the colon separator.
- Postgres backing store => token injection is one `psql -c "UPDATE …"`, no cp/restart dance.
