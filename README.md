# whoop-mcp

Local MCP server that pulls personal WHOOP data into SQLite and exposes it to Claude for natural-language querying.

## Why

The WHOOP app shows charts, but it's locked into the shapes WHOOP decided to build. With my data sitting in a local database behind an MCP server, I can ask Claude things like *"how did my recovery trend the week after I started lifting heavier?"* or *"compare my HRV on weeks I ran 4+ days vs. weeks I didn't"* and get a real answer against the full history — no API quota, no UI scrolling, no spreadsheet export.

## Architecture

Three layers, kept strictly separate:

```
WHOOP API  ──► sync layer ──► SQLite ──► MCP server ──► Claude
              (auth, client, sync)      (read-only tools)
```

The MCP server **never** hits the WHOOP API — only `sync_whoop_data` does, via the sync layer. Everything else answers from SQLite, so tool calls are instant and offline-friendly.

## Quick start

Requires Python 3.11+, [`uv`](https://docs.astral.sh/uv/), and a WHOOP developer app ([create one](https://developer.whoop.com/)).

```bash
cp .env.example .env          # then fill in WHOOP_CLIENT_ID / WHOOP_CLIENT_SECRET
uv sync
uv run whoop-auth             # one-time browser OAuth; persists rotating refresh token
uv run whoop-sync --backfill  # pulls full history into ./whoop.db
uv run whoop-mcp              # MCP stdio server
```

To register the server with Claude Code:

```bash
claude mcp add whoop-mcp -- uv --directory $(pwd) run python -m whoop_mcp.server
```

Then ask Claude something like *"sync my WHOOP data and tell me how my recovery trended over the last 30 days."*

## MCP tools

| Tool | What it does |
|---|---|
| `sync_whoop_data(since?)` | Pulls latest data from WHOOP into the local DB (the only tool that hits the API). |
| `get_recovery(start, end)` | Daily recovery scores joined to the night's sleep window. |
| `get_sleep(start, end)` | Sleep records (nights and naps), with derived `total_sleep_milli`. |
| `get_cycles(start, end)` | Physiological cycles — day strain, kJ, average/max HR. |
| `get_workouts(start, end, sport?)` | Workouts with optional sport filter. |
| `get_daily_summary(date)` | Recovery + main night-sleep + cycle + workouts for one date. |
| `compare_periods(start_a, end_a, start_b, end_b)` | Averaged metrics across two ranges with per-metric `delta`. |
| `get_trends(metric, window=7, days=90)` | Daily series + rolling mean for recovery, HRV, RHR, sleep performance/efficiency, or strain. |

## Tech stack

Python 3.11+ · `httpx` · MCP Python SDK (FastMCP) · SQLite (WAL) · `pydantic` · `python-dotenv`

## Notable implementation details

- **Rotating OAuth refresh tokens** persisted atomically with mode 0600 on every refresh — getting this wrong locks the integration out, so it's the most security-sensitive path in the codebase.
- **Transparent 401 retry** in the API client: a single failed call triggers a refresh and a retry, invisible to the caller.
- **Idempotent upserts** keyed on WHOOP's own UUIDs — backfill and incremental sync can re-run safely without duplicating rows.
- **2-day overlap on incremental sync** so late-finalized recovery scores get re-fetched (WHOOP's API filter is on record start, not on `updated_at`).
- **Three-layer separation enforced**: the MCP server reads only from SQLite. The sync process can run independently (cron, manual, or future webhook receiver) without touching the server.
- **WAL-mode SQLite** lets the MCP server and the sync process share the DB without locking each other out.

## Configuration

See [`.env.example`](.env.example). Required: `WHOOP_CLIENT_ID`, `WHOOP_CLIENT_SECRET`. Optional: `WHOOP_REDIRECT_URI` (must exactly match the one registered in the WHOOP dashboard), `WHOOP_DB_PATH`, `WHOOP_TOKENS_PATH`.

## Future work

Event-driven sync via WHOOP webhooks — a separate FastAPI receiver doing HMAC-SHA256 signature validation, fetching the referenced record, and upserting into the same DB the MCP server reads from.

## License

[MIT](LICENSE)
