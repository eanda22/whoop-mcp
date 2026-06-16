"""Historical backfill and (later, Phase 3) incremental sync.

Phase 2 ships `backfill()` — page every collection endpoint with the Phase 1
client and upsert the results into SQLite. Upserts are idempotent on the
WHOOP id, so re-running this is safe and converges instead of duplicating.

CLI:
    uv run python -m whoop_mcp.sync --backfill
    uv run python -m whoop_mcp.sync --backfill --start 2020-01-01
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

from . import db
from .client import WhoopClient
from .config import Config, load_config

DEFAULT_BACKFILL_YEARS = 5


def _parse_date(s: str) -> datetime:
    """Parse YYYY-MM-DD as midnight UTC. Datetimes must be aware for the client."""
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def backfill(
    start: datetime,
    end: datetime | None = None,
    *,
    cfg: Config | None = None,
) -> dict[str, int]:
    """Backfill all four resources from `start` to `end` (default: now).

    Returns per-resource record counts pulled from the API.
    """
    cfg = cfg or load_config()
    end = end or datetime.now(tz=timezone.utc)
    synced_at = datetime.now(tz=timezone.utc)

    counts: dict[str, int] = {}
    conn = db.connect(cfg.db_path)
    try:
        db.init_schema(conn)

        with WhoopClient(cfg) as client:
            print(f"backfill: {start.date()} -> {end.date()} (db: {cfg.db_path})")

            cycles = client.get_cycles(start, end)
            db.upsert_cycles(conn, cycles)
            db.set_sync_state(conn, "cycles", synced_at, db.max_updated_at(cycles))
            counts["cycles"] = len(cycles)
            print(f"  cycles:   {len(cycles)}")

            sleeps = client.get_sleep(start, end)
            db.upsert_sleep(conn, sleeps)
            db.set_sync_state(conn, "sleep", synced_at, db.max_updated_at(sleeps))
            counts["sleep"] = len(sleeps)
            print(f"  sleep:    {len(sleeps)}")

            workouts = client.get_workouts(start, end)
            db.upsert_workouts(conn, workouts)
            db.set_sync_state(conn, "workouts", synced_at, db.max_updated_at(workouts))
            counts["workouts"] = len(workouts)
            print(f"  workouts: {len(workouts)}")

            # Recovery missing for the most-recent unscored sleep is expected
            # — the collection endpoint just returns fewer records.
            recoveries = client.get_recovery(start, end)
            db.upsert_recovery(conn, recoveries)
            db.set_sync_state(conn, "recovery", synced_at, db.max_updated_at(recoveries))
            counts["recovery"] = len(recoveries)
            print(f"  recovery: {len(recoveries)}")
    finally:
        conn.close()

    return counts


def main() -> None:
    parser = argparse.ArgumentParser(prog="whoop-sync", description=__doc__)
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Run a historical backfill from --start to --end.",
    )
    parser.add_argument(
        "--start",
        type=_parse_date,
        default=None,
        help=f"Start date (YYYY-MM-DD). Defaults to {DEFAULT_BACKFILL_YEARS} years ago.",
    )
    parser.add_argument(
        "--end",
        type=_parse_date,
        default=None,
        help="End date (YYYY-MM-DD). Defaults to now (UTC).",
    )
    args = parser.parse_args()

    if not args.backfill:
        parser.error("--backfill is required (incremental sync lands in Phase 3).")

    start = args.start or (
        datetime.now(tz=timezone.utc) - timedelta(days=365 * DEFAULT_BACKFILL_YEARS)
    )
    backfill(start, args.end)


if __name__ == "__main__":
    main()
