"""Historical backfill and incremental sync.

- `backfill(start, end)` pages every collection endpoint from `start` to `end`
  and upserts the results. Use it once to seed the DB.
- `sync()` is the day-to-day driver: per resource, fetch from
  `last_synced_at - overlap_days` to now and upsert. Idempotent because the
  Phase 2 upserts key on the WHOOP id; the overlap re-fetch catches scores
  that finalize after we first saw them (recovery scoring after wake,
  late strain recalcs).

CLI:
    uv run whoop-sync                       # incremental
    uv run whoop-sync --overlap-days 7      # incremental, wider re-fetch
    uv run whoop-sync --backfill            # full history
    uv run whoop-sync --backfill --start 2020-01-01
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from typing import Callable, Sequence

from . import db
from .client import WhoopClient
from .config import Config, load_config

DEFAULT_BACKFILL_YEARS = 5
DEFAULT_OVERLAP_DAYS = 2


def _parse_date(s: str) -> datetime:
    """Parse YYYY-MM-DD as midnight UTC. Datetimes must be aware for the client."""
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


# Resource dispatch — keyed in the order we sync them. Recovery comes last so
# its window covers the freshest closed sleep cycle from this run.
_Fetch = Callable[[WhoopClient, datetime, datetime], Sequence]
_Upsert = Callable[..., int]

_RESOURCES: list[tuple[str, _Fetch, _Upsert]] = [
    ("cycles",   lambda c, s, e: c.get_cycles(s, e),   db.upsert_cycles),
    ("sleep",    lambda c, s, e: c.get_sleep(s, e),    db.upsert_sleep),
    ("workouts", lambda c, s, e: c.get_workouts(s, e), db.upsert_workouts),
    ("recovery", lambda c, s, e: c.get_recovery(s, e), db.upsert_recovery),
]


def _sync_resource(
    *,
    conn,
    client: WhoopClient,
    name: str,
    fetch: _Fetch,
    upsert: _Upsert,
    start: datetime,
    end: datetime,
    synced_at: datetime,
) -> int:
    """Fetch -> upsert -> advance sync_state. sync_state only advances on success."""
    records = fetch(client, start, end)
    upsert(conn, records)
    db.set_sync_state(conn, name, synced_at, db.max_updated_at(records))
    return len(records)


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
            for name, fetch, upsert in _RESOURCES:
                n = _sync_resource(
                    conn=conn, client=client, name=name,
                    fetch=fetch, upsert=upsert,
                    start=start, end=end, synced_at=synced_at,
                )
                counts[name] = n
                print(f"  {name:9s} {n}")
    finally:
        conn.close()

    return counts


def sync(
    *,
    cfg: Config | None = None,
    overlap_days: int = DEFAULT_OVERLAP_DAYS,
) -> dict[str, int]:
    """Incremental sync: each resource fetches from `last_synced_at - overlap_days` to now.

    Requires a prior `backfill()` so every resource has a `sync_state` row.
    Per-resource failures are caught so one bad resource doesn't poison the
    others; sync_state only advances on success, so a failed resource picks
    up from the same point on the next run.
    """
    cfg = cfg or load_config()
    end = datetime.now(tz=timezone.utc)
    synced_at = end
    overlap = timedelta(days=overlap_days)

    counts: dict[str, int] = {}
    conn = db.connect(cfg.db_path)
    try:
        db.init_schema(conn)

        # Validate sync_state up front so we fail fast (and consistently)
        # before doing any API work if backfill hasn't been run.
        starts: dict[str, datetime] = {}
        for name, _, _ in _RESOURCES:
            state = db.get_sync_state(conn, name)
            if state is None:
                raise RuntimeError(
                    f"no sync_state for {name!r}; run "
                    f"`whoop-sync --backfill` first."
                )
            last_synced = datetime.fromisoformat(state["last_synced_at"])
            starts[name] = last_synced - overlap

        with WhoopClient(cfg) as client:
            print(f"sync: overlap {overlap_days}d (db: {cfg.db_path})")
            for name, fetch, upsert in _RESOURCES:
                start = starts[name]
                try:
                    n = _sync_resource(
                        conn=conn, client=client, name=name,
                        fetch=fetch, upsert=upsert,
                        start=start, end=end, synced_at=synced_at,
                    )
                    counts[name] = n
                    print(f"  {name:9s} {n:5d}  (since {start.isoformat(timespec='seconds')})")
                except Exception as e:  # noqa: BLE001 — one bad resource shouldn't sink the others
                    counts[name] = -1
                    print(f"  {name:9s} FAILED: {e!r}")
    finally:
        conn.close()

    return counts


def main() -> None:
    parser = argparse.ArgumentParser(prog="whoop-sync", description=__doc__)
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Run a historical backfill instead of an incremental sync.",
    )
    parser.add_argument(
        "--start",
        type=_parse_date,
        default=None,
        help=f"(--backfill only) Start date (YYYY-MM-DD). Default: {DEFAULT_BACKFILL_YEARS} years ago.",
    )
    parser.add_argument(
        "--end",
        type=_parse_date,
        default=None,
        help="(--backfill only) End date (YYYY-MM-DD). Default: now (UTC).",
    )
    parser.add_argument(
        "--overlap-days",
        type=int,
        default=DEFAULT_OVERLAP_DAYS,
        help=(
            "(incremental only) Re-fetch this many days back from last sync "
            f"to catch late score updates. Default: {DEFAULT_OVERLAP_DAYS}."
        ),
    )
    args = parser.parse_args()

    if args.backfill:
        start = args.start or (
            datetime.now(tz=timezone.utc) - timedelta(days=365 * DEFAULT_BACKFILL_YEARS)
        )
        backfill(start, args.end)
    else:
        sync(overlap_days=args.overlap_days)


if __name__ == "__main__":
    main()
