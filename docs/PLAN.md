# WHOOP MCP Server — Build Plan

A spec for building a local MCP server that pulls personal WHOOP data into a
local database and exposes it to Claude through MCP tools. Built for a single
user (the developer). Feed this to Claude Code one phase at a time, in order.

---

## Goal

Let Claude answer questions about my WHOOP sleep, recovery, strain, and workout
data. Data is stored in a local database so historical comparisons do not require
repeated API calls. The database is the source of truth; a separate sync keeps it
fresh.

## Tech stack

- Python 3.11+
- `httpx` for API calls
- MCP Python SDK (`mcp`, using FastMCP) for the server
- SQLite (via `sqlite3` or SQLModel/SQLAlchemy) for storage
- `pydantic` for typed data models
- `python-dotenv` for config
- Use `uv` for env and dependency management if available, otherwise `pip` + venv

## Architecture (three layers, kept separate)

1. **Sync layer** — talks to the WHOOP API, normalizes responses, upserts into the DB.
2. **Database** — the source of truth for all historical data.
3. **MCP server** — tools that read from the DB (not WHOOP directly), plus one tool to trigger a sync.

This separation is deliberate. MCP tools answer from local data instantly, and
syncing is an independent concern that can later be scheduled or driven by webhooks.

---

## WHOOP API facts to respect (verify exact paths/schemas against the docs)

Base URL: `https://api.prod.whoop.com`
Auth URL: `https://api.prod.whoop.com/oauth/oauth2/auth`
Token URL: `https://api.prod.whoop.com/oauth/oauth2/token`
Docs: https://developer.whoop.com/docs (use these to confirm exact endpoint paths and response field names before writing models)

- **API version is v2.** Endpoints live under `/v2/...` (e.g. cycle, recovery, sleep, workout, user profile, body measurement). Confirm the exact paths in the docs.
- **OAuth 2.0 authorization code flow.** Request scopes including `offline` (required to receive a refresh token), plus `read:profile`, `read:recovery`, `read:sleep`, `read:cycles`, `read:workout`, `read:body_measurement`. Confirm scope strings in the docs.
- **Refresh tokens ROTATE.** Every token refresh returns a NEW refresh token that replaces the old one. The code MUST persist the new refresh token on every refresh or the integration locks out. Treat this as a hard requirement.
- **Recovery is a morning metric.** A recovery score does not exist until the preceding sleep cycle closes. Missing recovery for "today" before wake is normal, not an error.
- **Cycle-based data model.** Data is organized around sleep, recovery, and strain cycles. In v2, recovery is reached through the cycle data. Mirror this structure in the schema rather than flattening to one row per day.
- **Pagination caps at 25 records per call.** Collection endpoints use a `nextToken` cursor and `start`/`end` ISO 8601 time filters. Historical backfill must loop through pages.
- **The redirect URI in each auth request must EXACTLY match** the one registered in the dashboard (`http://localhost:8080/callback` or whatever is registered).
- **Not available via API:** GPS, step counts, continuous heart rate. Do not design around these.

---

## Configuration

`.env` (gitignored — confirm `.env` is in `.gitignore` BEFORE creating it):

```
WHOOP_CLIENT_ID=...
WHOOP_CLIENT_SECRET=...
WHOOP_REDIRECT_URI=http://localhost:8080/callback
WHOOP_DB_PATH=./whoop.db
```

Tokens (access token, refresh token, expiry) are persisted to the database or a
separate gitignored token file — never committed.

---

## Build phases

### Phase 0 — Project setup + OAuth
- Initialize the repo structure, dependencies, `.gitignore` (include `.env`, `*.db`, token files).
- Implement the authorization code flow:
  - Build the authorization URL with the required scopes and `offline`.
  - Run a tiny local HTTP listener on the redirect URI to catch the `code`.
  - Exchange the code at the token URL for an access token + refresh token.
  - Persist both tokens and the expiry.
- Implement token refresh that ALWAYS saves the returned (new) refresh token.
- Deliverable: a script I can run once to authorize, after which tokens persist and auto-refresh.

### Phase 1 — WHOOP API client
- A typed client wrapping each endpoint: cycles, recovery, sleep, workouts, user profile, body measurement.
- Automatic bearer-token injection and transparent refresh on 401/expiry.
- Pagination helper that loops `nextToken` to pull full ranges (respect the 25-record cap).
- Pydantic models for each response type (confirm field names against the docs).
- Deliverable: functions like `get_sleep(start, end)` that return clean typed objects.

### Phase 2 — Database
- SQLite schema with tables roughly: `cycles`, `recovery`, `sleep`, `workouts`, plus `sync_state` and a tokens store.
- Use WHOOP's own IDs (UUIDs in v2) as primary keys so re-syncing upserts rather than duplicates.
- Idempotent upsert functions for each table.
- A historical backfill routine that pages all the way back to pull existing history.
- Deliverable: run backfill once and have the full history sitting locally.

### Phase 3 — Sync logic
- Incremental sync using `sync_state` (last synced timestamp / cursor per resource) so only new data is fetched.
- Handle the "recovery not yet available" case gracefully.
- Deliverable: a `sync()` entrypoint that brings the DB up to date quickly.

### Phase 4 — MCP server
- A FastMCP server exposing tools that READ FROM THE DB:
  - `sync_whoop_data(since?)` — trigger a sync (the one tool that hits the API).
  - `get_recovery(start, end)`
  - `get_sleep(start, end)`
  - `get_strain(start, end)` / `get_cycles(start, end)`
  - `get_workouts(start, end, sport?)`
  - `get_daily_summary(date)` — recovery + sleep + strain for one day.
  - `compare_periods(period_a, period_b)` — averaged metrics across two ranges.
  - `get_trends(metric, window)` — rolling averages over time.
- Register the server in the Claude client config.
- Deliverable: ask Claude questions and get answers from local data.

### Phase 5 — Webhooks (event-driven sync)

Replace polling with WHOOP webhooks: when WHOOP has new data, it pushes a small
notification to an HTTPS endpoint I host, which fetches the updated record and
upserts it into the same database the MCP server reads from. This makes the data
near real time without constant API polling, and demonstrates event-driven design
and webhook security.

Confirm all exact paths, header names, event-type strings, and payload fields
against https://developer.whoop.com/docs/developing/webhooks before finalizing.

#### How WHOOP webhooks work (the important facts)

- **v2 webhooks only.** v1 webhooks have been removed. In the dashboard, set the webhook URL's Model Version to v2.
- **A webhook is a notification, not the data.** The POST body is small — roughly `{ "user_id", "id", "type", "trace_id" }`. It tells me *what changed* and gives an ID. I still call the WHOOP API with that ID to fetch the actual updated record, then upsert it. So Phase 5 reuses the Phase 1 client and Phase 2 upserts.
- **v2 IDs are UUIDs.** Activity events (sleep, workout) carry the resource's UUID. Recovery events carry the UUID of the *associated sleep*, not a cycle ID — handle that mapping when fetching recovery.
- **Event types** look like `sleep.updated`, `workout.updated`, `recovery.updated` (confirm the full list in the docs). All event types are delivered to every configured URL; for ones I don't care about, just return a 2XX and ignore them.
- **The endpoint must be reachable over public HTTPS.** This is the main practical hurdle for a local project — see the hosting note below.
- **Signature validation is required.** Because the URL is public, anyone could POST forged events. WHOOP signs each request so I can verify it genuinely came from WHOOP.
- **Webhooks can be missed** (downtime, delivery failures). WHOOP recommends also running a periodic reconciliation job — which I already have in the form of the Phase 3 incremental sync. Keep that as a scheduled backstop.

#### Signature validation (how it works)

WHOOP sends a signature header and a timestamp header with each request (confirm
the exact header names in the docs). To verify:

1. Take the raw, unparsed HTTP request body (do not re-serialize the JSON — byte-for-byte matters).
2. Prepend the timestamp header value to that raw body.
3. Compute an HMAC-SHA256 of the result using the app's **client secret** as the key.
4. Base64- or hex-encode it per the docs and compare against the signature header using a constant-time comparison (`hmac.compare_digest`).
5. If it doesn't match, reject with 4XX and do not process the event.

#### Handling pattern (do this in the receiver)

1. Read the raw body and headers.
2. Validate the signature. Reject if invalid.
3. Respond 2XX immediately — acknowledge fast, before doing real work.
4. Do the actual fetch-and-upsert in a background task or queue, not inline, so slow API calls never delay the acknowledgement.
5. Treat events as possibly duplicated and possibly out of order — upserts keyed on the WHOOP UUID already make this safe (idempotent).

#### Hosting the endpoint

The MCP server runs locally on demand, but a webhook receiver has to be a
long-running, publicly reachable HTTPS server. Two ways to handle that:

- **Local development:** run a small FastAPI/Flask receiver locally and expose it with a tunnel like ngrok or Cloudflare Tunnel, which gives a public HTTPS URL pointing at the local port. Register that URL in the dashboard. Good for testing the flow.
- **Persistent setup:** deploy the receiver to a small always-on host (a cheap VPS, a serverless function, or a container) so it keeps receiving events when my laptop is off. The receiver writes to the same database the MCP server reads.

Note the architecture: the webhook receiver is a **separate process** from the
MCP server. Receiver writes to the DB, MCP server reads from it. They share only
the database, which keeps the three-layer separation intact.

#### Phase 5 deliverables

- A FastAPI (or Flask) HTTPS receiver with one POST route for webhooks.
- Signature validation middleware/function.
- Per-event-type handlers that fetch the updated record (reusing the Phase 1 client) and upsert it (reusing Phase 2).
- Background processing so the endpoint acknowledges fast.
- Webhook URL registered in the dashboard with Model Version set to v2.
- Phase 3 incremental sync kept on a schedule as a reconciliation backstop.

---

## Later (after the first five phases)

Once the core plus webhooks are working, these are the natural next directions —
not part of the initial build:

- **Correlation engine** — compute relationships within my own data (late workout vs. sleep performance, consecutive high-strain days vs. recovery, weekday HRV trends) and expose them as MCP tools so Claude can reason over the findings.
- **Calendar correlation** — join recovery/sleep against Google Calendar events to surface schedule-driven patterns (travel, meeting-heavy days). Joins two data sources.
- **Dashboard** — a small web view of trends over time, making the project visual and screenshottable.

---

## Suggested repo structure

```
whoop-mcp/
  .env                  # gitignored
  .gitignore
  README.md             # includes the privacy note
  pyproject.toml
  src/
    whoop_mcp/
      config.py         # loads env
      auth.py           # OAuth flow + rotating-token refresh
      client.py         # WHOOP API client + pagination
      models.py         # pydantic models
      db.py             # schema + upserts + sync_state
      sync.py           # backfill + incremental sync
      server.py         # FastMCP server + tools
      webhooks.py       # Phase 5 — FastAPI receiver + signature validation
  whoop.db              # gitignored
```

## Notes for the implementer
- Confirm every exact endpoint path, scope string, and response field name against https://developer.whoop.com/docs before finalizing models. Do not assume.
- The rotating refresh token is the most common failure point — get persistence right in Phase 0 and test it by forcing a refresh.
- Keep secrets out of the repo. Never log the client secret or tokens.