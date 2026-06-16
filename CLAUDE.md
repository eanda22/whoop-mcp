# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

This repo is in the **pre-implementation phase**. The only authoritative content is `docs/PLAN.md`, a detailed build spec for a local MCP server that ingests personal WHOOP data into SQLite and exposes it to Claude via FastMCP tools. Read `docs/PLAN.md` in full before making changes — it defines the architecture, phases, and the API constraints listed below.

When building, follow the phase order in `docs/PLAN.md` (Phase 0 OAuth → 1 API client → 2 DB → 3 sync → 4 MCP server → 5 webhooks). Do not skip ahead.

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

`.env` is gitignored. Confirm `.env`, `*.db`, and token files are in `.gitignore` BEFORE creating them. Required keys:

```
WHOOP_CLIENT_ID=
WHOOP_CLIENT_SECRET=
WHOOP_REDIRECT_URI=http://localhost:8080/callback
WHOOP_DB_PATH=./whoop.db
```

Never log the client secret or tokens.

## Commands

Once `pyproject.toml` exists, expected commands (update this section as tooling lands):

- Install: `uv sync` (or `pip install -e .`)
- Run OAuth bootstrap: `uv run python -m whoop_mcp.auth`
- Run backfill: `uv run python -m whoop_mcp.sync --backfill`
- Run incremental sync: `uv run python -m whoop_mcp.sync`
- Run MCP server: `uv run python -m whoop_mcp.server`
- Run webhook receiver (Phase 5): `uv run uvicorn whoop_mcp.webhooks:app --port 8081`
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
