# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

Phases 0–3 are complete and committed: OAuth with rotating-refresh persistence (`auth.py`, `tokens.py`), typed v2 API client with pagination + 401 retry (`client.py`, `models.py`), SQLite schema with idempotent upserts and `sync_state` (`db.py`), and backfill + incremental sync (`sync.py`). Next up is Phase 4 (MCP server, `server.py`), then Phase 5 (webhooks, `webhooks.py`).

`docs/PLAN.md` is the authoritative build spec — read it in full before making changes. Follow the phase order; do not skip ahead.

## Architecture (must preserve three-layer separation)

1. **Sync layer** (`auth.py`, `client.py`, `sync.py`) — talks to the WHOOP API, normalizes responses, upserts into the DB.
2. **Database** (`db.py`) — SQLite, the source of truth for all historical data. Uses WHOOP's own UUIDs as primary keys so re-syncing upserts rather than duplicates.
3. **MCP server** (`server.py`) — FastMCP tools that READ FROM THE DB, not from the WHOOP API. The only tool that hits the API is `sync_whoop_data`.

The Phase 5 webhook receiver (`webhooks.py`) is a **separate process** from the MCP server. They share only the database. Do not merge them.

## WHOOP API constraints (easy to get wrong)

- **API is v2.** Endpoints under `/v2/...`. Base URL `https://api.prod.whoop.com`.
- **Refresh tokens ROTATE.** Every refresh returns a new refresh token that replaces the old one. Persisting the new token on every refresh is a hard requirement — getting this wrong locks the integration out.
- **OAuth scope `offline` is required** to receive a refresh token at all.
- **Pagination caps at 25 records per call** with a `nextToken` cursor. Backfill must loop.
- **Recovery is a morning metric** — missing recovery for "today" before wake is expected, not an error.
- **Redirect URI must EXACTLY match** the one registered in the WHOOP dashboard.
- **Not exposed by the API:** GPS, step counts, continuous HR. Don't design around them.
- **Webhook signature validation** must use the RAW request body bytes (do not re-serialize the JSON) prepended with the timestamp header, HMAC-SHA256'd with the client secret. Compare with `hmac.compare_digest`.
- **Webhook payloads are notifications, not data** — they carry an ID; you still call the API to fetch the record. Recovery events carry the associated *sleep* UUID, not a cycle ID.

## Tech stack

Python 3.11+, `httpx`, MCP Python SDK (`mcp`, FastMCP), SQLite, `pydantic`, `python-dotenv`. Phase 5 adds FastAPI (or Flask) for the webhook receiver. Prefer `uv` for env/deps if available, else `pip` + venv.

## Configuration

`.env` is gitignored. Required keys:

```
WHOOP_CLIENT_ID=
WHOOP_CLIENT_SECRET=
WHOOP_REDIRECT_URI=http://localhost:8080/callback
WHOOP_DB_PATH=./whoop.db          # optional, this is the default
WHOOP_TOKENS_PATH=./.whoop_tokens.json  # optional, this is the default
```

Never log the client secret or tokens.

## Persistence (durable facts for later phases)

- **Tokens** live at `WHOOP_TOKENS_PATH` (default `./.whoop_tokens.json`), written atomically with mode 0600, gitignored. Every refresh persists the rotated refresh token in place; the file is the integration's single source of authentication truth.
- **Database** lives at `WHOOP_DB_PATH` (default `./whoop.db`), WAL mode, gitignored. Connections opened via `db.connect()` use autocommit + explicit `db.transaction()` blocks.
- **Primary keys** are WHOOP's own ids: `cycles.id` int, `sleep.id` / `workouts.id` UUID, `recovery.sleep_id` UUID (recovery is keyed by the associated *sleep* UUID, not by cycle). Re-fetching the same record upserts in place.
- **`sync_state`** is keyed by the resource-name string `"cycles" | "sleep" | "workouts" | "recovery"`. It stores `last_synced_at` (wall clock at sync time) and `last_record_updated_at` (max `updated_at` from the batch — informational; not used as a filter). Incremental sync derives the next API window as `last_synced_at - overlap_days` (default 2) → now, with one-day-plus overlap so late-finalized scores get re-fetched. The WHOOP API filter is on the record's start timestamp, not on `updated_at`, which is why the overlap matters.

## Commands

- Install: `uv sync`
- One-time OAuth authorization (opens a browser, catches the redirect, persists tokens): `uv run whoop-auth`
- Smoke-test the API client against a small window: `uv run whoop-client` (defaults to last 7 days)
- Historical backfill (run once after `whoop-auth`): `uv run whoop-sync --backfill` (or `--backfill --start YYYY-MM-DD`)
- Incremental sync (daily driver): `uv run whoop-sync` (or `whoop-sync --overlap-days 7` to widen the re-fetch window)
- Run MCP server (Phase 4, TODO): `uv run python -m whoop_mcp.server`
- Run webhook receiver (Phase 5, TODO): `uv run uvicorn whoop_mcp.webhooks:app --port 8081`
- Tests: `uv run pytest` (single test: `uv run pytest path/to/test.py::test_name`)

## Verification

Before declaring a phase complete, prove it end-to-end:
- Phase 0: force a token refresh and confirm the NEW refresh token is persisted.
- Phase 2/3: run backfill, then run sync again and confirm no duplicate rows (upsert correctness).
- Phase 4: register the server in the Claude MCP config and ask a question that exercises the DB.
- Phase 5: send a signed test payload and a deliberately-bad-signature payload — second must be rejected with 4XX.

## Commit style

Commit messages in this repo are **one line only**. No body, no trailing
sign-off block. Keep the subject specific (what changed, not just "update"),
under ~72 chars when possible.
