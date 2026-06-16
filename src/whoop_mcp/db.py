"""SQLite store for WHOOP cycles, recovery, sleep, and workouts.

Source of truth for all historical data. Tables use WHOOP's own IDs as
primary keys, so every write is an idempotent upsert keyed on the WHOOP UUID
(or integer cycle id). Re-syncing the same record updates it in place; it
never inserts a duplicate.

Score blocks are flattened into nullable columns on the parent table.
`score_state in ('PENDING_SCORE', 'UNSCORABLE')` is represented as all
`score_*` columns being NULL.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from .models import Cycle, Recovery, Sleep, Workout

SCHEMA = """
CREATE TABLE IF NOT EXISTS cycles (
    id                          INTEGER PRIMARY KEY,
    user_id                     INTEGER NOT NULL,
    created_at                  TEXT    NOT NULL,
    updated_at                  TEXT    NOT NULL,
    start                       TEXT    NOT NULL,
    end                         TEXT,
    timezone_offset             TEXT    NOT NULL,
    score_state                 TEXT    NOT NULL,
    score_strain                REAL,
    score_kilojoule             REAL,
    score_average_heart_rate    INTEGER,
    score_max_heart_rate        INTEGER
);
CREATE INDEX IF NOT EXISTS ix_cycles_start ON cycles(start);

CREATE TABLE IF NOT EXISTS recovery (
    sleep_id                    TEXT    PRIMARY KEY,
    cycle_id                    INTEGER NOT NULL,
    user_id                     INTEGER NOT NULL,
    created_at                  TEXT    NOT NULL,
    updated_at                  TEXT    NOT NULL,
    score_state                 TEXT    NOT NULL,
    score_user_calibrating      INTEGER,
    score_recovery_score        REAL,
    score_resting_heart_rate    REAL,
    score_hrv_rmssd_milli       REAL,
    score_spo2_percentage       REAL,
    score_skin_temp_celsius     REAL
);
CREATE INDEX IF NOT EXISTS ix_recovery_cycle ON recovery(cycle_id);

CREATE TABLE IF NOT EXISTS sleep (
    id                                      TEXT    PRIMARY KEY,
    v1_id                                   INTEGER,
    cycle_id                                INTEGER,
    user_id                                 INTEGER NOT NULL,
    created_at                              TEXT    NOT NULL,
    updated_at                              TEXT    NOT NULL,
    start                                   TEXT    NOT NULL,
    end                                     TEXT    NOT NULL,
    timezone_offset                         TEXT    NOT NULL,
    nap                                     INTEGER NOT NULL,
    score_state                             TEXT    NOT NULL,
    score_total_in_bed_time_milli           INTEGER,
    score_total_awake_time_milli            INTEGER,
    score_total_no_data_time_milli          INTEGER,
    score_total_light_sleep_time_milli      INTEGER,
    score_total_slow_wave_sleep_time_milli  INTEGER,
    score_total_rem_sleep_time_milli        INTEGER,
    score_sleep_cycle_count                 INTEGER,
    score_disturbance_count                 INTEGER,
    score_baseline_milli                    INTEGER,
    score_need_from_sleep_debt_milli        INTEGER,
    score_need_from_recent_strain_milli     INTEGER,
    score_need_from_recent_nap_milli        INTEGER,
    score_respiratory_rate                  REAL,
    score_sleep_performance_percentage      REAL,
    score_sleep_consistency_percentage      REAL,
    score_sleep_efficiency_percentage       REAL
);
CREATE INDEX IF NOT EXISTS ix_sleep_start ON sleep(start);
CREATE INDEX IF NOT EXISTS ix_sleep_cycle ON sleep(cycle_id);

CREATE TABLE IF NOT EXISTS workouts (
    id                              TEXT    PRIMARY KEY,
    v1_id                           INTEGER,
    user_id                         INTEGER NOT NULL,
    created_at                      TEXT    NOT NULL,
    updated_at                      TEXT    NOT NULL,
    start                           TEXT    NOT NULL,
    end                             TEXT    NOT NULL,
    timezone_offset                 TEXT    NOT NULL,
    sport_name                      TEXT    NOT NULL,
    sport_id                        INTEGER,
    score_state                     TEXT    NOT NULL,
    score_strain                    REAL,
    score_average_heart_rate        INTEGER,
    score_max_heart_rate            INTEGER,
    score_kilojoule                 REAL,
    score_percent_recorded          REAL,
    score_distance_meter            REAL,
    score_altitude_gain_meter       REAL,
    score_altitude_change_meter     REAL,
    score_zone_zero_milli           INTEGER,
    score_zone_one_milli            INTEGER,
    score_zone_two_milli            INTEGER,
    score_zone_three_milli          INTEGER,
    score_zone_four_milli           INTEGER,
    score_zone_five_milli           INTEGER
);
CREATE INDEX IF NOT EXISTS ix_workouts_start ON workouts(start);

CREATE TABLE IF NOT EXISTS sync_state (
    resource                TEXT    PRIMARY KEY,
    last_synced_at          TEXT    NOT NULL,
    last_record_updated_at  TEXT
);
"""


# ---------- connection ----------


def connect(path: Path) -> sqlite3.Connection:
    """Open a SQLite connection with WAL + foreign keys enabled."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)  # autocommit; we use explicit transactions
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# ---------- helpers ----------


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat()


def _upsert_sql(table: str, columns: list[str], pk: str) -> str:
    placeholders = ", ".join("?" * len(columns))
    non_pk = [c for c in columns if c != pk]
    set_clause = ", ".join(f"{c}=excluded.{c}" for c in non_pk)
    return (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT({pk}) DO UPDATE SET {set_clause}"
    )


def _bulk_upsert(
    conn: sqlite3.Connection,
    table: str,
    columns: list[str],
    pk: str,
    rows: list[tuple],
) -> None:
    if not rows:
        return
    sql = _upsert_sql(table, columns, pk)
    with transaction(conn):
        conn.executemany(sql, rows)


# ---------- row builders ----------


_CYCLE_COLUMNS = [
    "id", "user_id", "created_at", "updated_at", "start", "end",
    "timezone_offset", "score_state",
    "score_strain", "score_kilojoule",
    "score_average_heart_rate", "score_max_heart_rate",
]


def _cycle_row(c: Cycle) -> tuple:
    s = c.score
    return (
        c.id, c.user_id, _iso(c.created_at), _iso(c.updated_at),
        _iso(c.start), _iso(c.end), c.timezone_offset, c.score_state,
        s.strain if s else None,
        s.kilojoule if s else None,
        s.average_heart_rate if s else None,
        s.max_heart_rate if s else None,
    )


_RECOVERY_COLUMNS = [
    "sleep_id", "cycle_id", "user_id", "created_at", "updated_at",
    "score_state",
    "score_user_calibrating", "score_recovery_score",
    "score_resting_heart_rate", "score_hrv_rmssd_milli",
    "score_spo2_percentage", "score_skin_temp_celsius",
]


def _recovery_row(r: Recovery) -> tuple:
    s = r.score
    return (
        r.sleep_id, r.cycle_id, r.user_id,
        _iso(r.created_at), _iso(r.updated_at), r.score_state,
        int(s.user_calibrating) if s else None,
        s.recovery_score if s else None,
        s.resting_heart_rate if s else None,
        s.hrv_rmssd_milli if s else None,
        s.spo2_percentage if s else None,
        s.skin_temp_celsius if s else None,
    )


_SLEEP_COLUMNS = [
    "id", "v1_id", "cycle_id", "user_id", "created_at", "updated_at",
    "start", "end", "timezone_offset", "nap", "score_state",
    "score_total_in_bed_time_milli", "score_total_awake_time_milli",
    "score_total_no_data_time_milli", "score_total_light_sleep_time_milli",
    "score_total_slow_wave_sleep_time_milli", "score_total_rem_sleep_time_milli",
    "score_sleep_cycle_count", "score_disturbance_count",
    "score_baseline_milli", "score_need_from_sleep_debt_milli",
    "score_need_from_recent_strain_milli", "score_need_from_recent_nap_milli",
    "score_respiratory_rate", "score_sleep_performance_percentage",
    "score_sleep_consistency_percentage", "score_sleep_efficiency_percentage",
]


def _sleep_row(sl: Sleep) -> tuple:
    s = sl.score
    stages = s.stage_summary if s else None
    need = s.sleep_needed if s else None
    return (
        sl.id, sl.v1_id, sl.cycle_id, sl.user_id,
        _iso(sl.created_at), _iso(sl.updated_at),
        _iso(sl.start), _iso(sl.end), sl.timezone_offset,
        int(sl.nap), sl.score_state,
        stages.total_in_bed_time_milli if stages else None,
        stages.total_awake_time_milli if stages else None,
        stages.total_no_data_time_milli if stages else None,
        stages.total_light_sleep_time_milli if stages else None,
        stages.total_slow_wave_sleep_time_milli if stages else None,
        stages.total_rem_sleep_time_milli if stages else None,
        stages.sleep_cycle_count if stages else None,
        stages.disturbance_count if stages else None,
        need.baseline_milli if need else None,
        need.need_from_sleep_debt_milli if need else None,
        need.need_from_recent_strain_milli if need else None,
        need.need_from_recent_nap_milli if need else None,
        s.respiratory_rate if s else None,
        s.sleep_performance_percentage if s else None,
        s.sleep_consistency_percentage if s else None,
        s.sleep_efficiency_percentage if s else None,
    )


_WORKOUT_COLUMNS = [
    "id", "v1_id", "user_id", "created_at", "updated_at",
    "start", "end", "timezone_offset", "sport_name", "sport_id",
    "score_state",
    "score_strain", "score_average_heart_rate", "score_max_heart_rate",
    "score_kilojoule", "score_percent_recorded",
    "score_distance_meter", "score_altitude_gain_meter", "score_altitude_change_meter",
    "score_zone_zero_milli", "score_zone_one_milli", "score_zone_two_milli",
    "score_zone_three_milli", "score_zone_four_milli", "score_zone_five_milli",
]


def _workout_row(w: Workout) -> tuple:
    s = w.score
    z = s.zone_durations if s else None
    return (
        w.id, w.v1_id, w.user_id,
        _iso(w.created_at), _iso(w.updated_at),
        _iso(w.start), _iso(w.end), w.timezone_offset,
        w.sport_name, w.sport_id, w.score_state,
        s.strain if s else None,
        s.average_heart_rate if s else None,
        s.max_heart_rate if s else None,
        s.kilojoule if s else None,
        s.percent_recorded if s else None,
        s.distance_meter if s else None,
        s.altitude_gain_meter if s else None,
        s.altitude_change_meter if s else None,
        z.zone_zero_milli if z else None,
        z.zone_one_milli if z else None,
        z.zone_two_milli if z else None,
        z.zone_three_milli if z else None,
        z.zone_four_milli if z else None,
        z.zone_five_milli if z else None,
    )


# ---------- public upserts ----------


def upsert_cycles(conn: sqlite3.Connection, cycles: Iterable[Cycle]) -> int:
    rows = [_cycle_row(c) for c in cycles]
    _bulk_upsert(conn, "cycles", _CYCLE_COLUMNS, "id", rows)
    return len(rows)


def upsert_recovery(conn: sqlite3.Connection, recoveries: Iterable[Recovery]) -> int:
    rows = [_recovery_row(r) for r in recoveries]
    _bulk_upsert(conn, "recovery", _RECOVERY_COLUMNS, "sleep_id", rows)
    return len(rows)


def upsert_sleep(conn: sqlite3.Connection, sleeps: Iterable[Sleep]) -> int:
    rows = [_sleep_row(s) for s in sleeps]
    _bulk_upsert(conn, "sleep", _SLEEP_COLUMNS, "id", rows)
    return len(rows)


def upsert_workouts(conn: sqlite3.Connection, workouts: Iterable[Workout]) -> int:
    rows = [_workout_row(w) for w in workouts]
    _bulk_upsert(conn, "workouts", _WORKOUT_COLUMNS, "id", rows)
    return len(rows)


# ---------- sync_state ----------


def get_sync_state(conn: sqlite3.Connection, resource: str) -> sqlite3.Row | None:
    row = conn.execute(
        "SELECT resource, last_synced_at, last_record_updated_at "
        "FROM sync_state WHERE resource = ?",
        (resource,),
    ).fetchone()
    return row


def set_sync_state(
    conn: sqlite3.Connection,
    resource: str,
    last_synced_at: datetime,
    last_record_updated_at: datetime | None,
) -> None:
    conn.execute(
        "INSERT INTO sync_state (resource, last_synced_at, last_record_updated_at) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT(resource) DO UPDATE SET "
        "last_synced_at = excluded.last_synced_at, "
        "last_record_updated_at = excluded.last_record_updated_at",
        (resource, _iso(last_synced_at), _iso(last_record_updated_at)),
    )


def max_updated_at(records: Iterable) -> datetime | None:
    """Return the latest `updated_at` across a batch of records, or None."""
    latest: datetime | None = None
    for r in records:
        ts = getattr(r, "updated_at", None)
        if ts is not None and (latest is None or ts > latest):
            latest = ts
    return latest
