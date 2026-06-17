"""WHOOP MCP server — exposes the local SQLite DB as MCP tools.

All tools read from the DB except `sync_whoop_data`, which delegates to the
Phase 3 sync layer. Run as: `uv run whoop-mcp` (stdio transport).
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Literal

from mcp.server.fastmcp import FastMCP

from . import db, sync as sync_mod
from .config import load_config

# stdout is reserved for JSON-RPC under stdio transport; route all logs to stderr.
logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("whoop-mcp")

mcp = FastMCP("whoop-mcp")
_cfg = load_config()


# ---------- date / connection helpers ----------


def _parse_date(s: str) -> datetime:
    """Parse 'YYYY-MM-DD' as UTC midnight."""
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def _range(start: str, end: str) -> tuple[str, str]:
    """[start_date, end_date] (inclusive days) -> ISO strings for half-open SQL.

    The DB stores UTC ISO 8601 strings; lexicographic comparison sorts them
    correctly, so we use plain `>= start_iso AND < end_iso_next`.
    """
    s = _parse_date(start)
    e = _parse_date(end) + timedelta(days=1)
    return s.isoformat(), e.isoformat()


def _day_range(date: str) -> tuple[str, str]:
    """Single-day range [date 00:00Z, (date+1) 00:00Z)."""
    return _range(date, date)


@contextlib.contextmanager
def _conn():
    conn = db.connect(_cfg.db_path)
    try:
        yield conn
    finally:
        conn.close()


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()


def _strip_score_prefix(row: sqlite3.Row | dict) -> dict[str, Any]:
    """Convert a row to a dict and drop the noisy 'score_' prefix on keys.

    `score_state` is preserved as-is — stripped to bare 'state' it would be
    ambiguous (state of what?), so we keep the prefix for that one column.
    """
    out: dict[str, Any] = {}
    for k, v in dict(row).items():
        if k == "score_state":
            out[k] = v
        elif k.startswith("score_"):
            out[k[6:]] = v
        else:
            out[k] = v
    return out


def _parse_tz_offset(offset: str | None) -> timezone | None:
    """Parse a WHOOP timezone_offset like '-07:00' or '+05:30' into a tzinfo."""
    if not offset or len(offset) < 6 or offset[0] not in "+-":
        return None
    try:
        hours = int(offset[1:3])
        minutes = int(offset[4:6])
    except ValueError:
        return None
    sign = 1 if offset[0] == "+" else -1
    return timezone(timedelta(hours=sign * hours, minutes=sign * minutes))


def _add_local_times(rec: dict[str, Any]) -> dict[str, Any]:
    """For any UTC ISO timestamp paired with a `timezone_offset`, add a `<name>_local`
    sibling expressed in that local zone. Stored times remain UTC; local fields
    are purely for unambiguous human-time presentation by the LLM.
    """
    tz = _parse_tz_offset(rec.get("timezone_offset"))
    if tz is None:
        return rec
    for key in ("start", "end", "sleep_start", "sleep_end"):
        val = rec.get(key)
        if not val:
            continue
        try:
            rec[f"{key}_local"] = datetime.fromisoformat(val).astimezone(tz).isoformat()
        except ValueError:
            pass
    return rec


# ---------- read-only tools ----------


@mcp.tool()
def get_recovery(start: str, end: str) -> dict:
    """Daily recovery scores for an inclusive date range (YYYY-MM-DD .. YYYY-MM-DD).

    Each record includes recovery_score (0-100), hrv_rmssd_milli, resting_heart_rate,
    spo2_percentage, skin_temp_celsius, plus the sleep_start/sleep_end of the
    associated night. score_state is 'SCORED' for finalized records or
    'PENDING_SCORE'/'UNSCORABLE' (in which case score fields will be null).

    Use for questions like 'what was my recovery last week?' or 'how was my
    recovery on a specific date?'.
    """
    s, e = _range(start, end)
    sql = """
        SELECT r.sleep_id, r.cycle_id, r.score_state,
               r.score_recovery_score, r.score_hrv_rmssd_milli,
               r.score_resting_heart_rate, r.score_spo2_percentage, r.score_skin_temp_celsius,
               s.start AS sleep_start, s.end AS sleep_end, s.nap,
               s.timezone_offset
        FROM recovery r
        JOIN sleep s ON s.id = r.sleep_id
        WHERE s.start >= ? AND s.start < ?
        ORDER BY s.start
    """
    with _conn() as conn:
        rows = _rows(conn, sql, (s, e))
    records = [_add_local_times(_strip_score_prefix(r)) for r in rows]
    return {"count": len(records), "records": records}


@mcp.tool()
def get_sleep(start: str, end: str) -> dict:
    """All sleep records (nights AND naps) starting in an inclusive date range.

    Each record includes start/end, nap flag, score_state, and the score block:
    total_in_bed_time_milli, total_light/sws/rem_sleep_time_milli, performance %,
    consistency %, efficiency %, respiratory_rate, sleep_needed components.
    A derived `total_sleep_milli` (light + sws + rem) is included for convenience.

    To get only main nights, filter for `nap == 0` in the response.
    """
    s, e = _range(start, end)
    sql = "SELECT * FROM sleep WHERE start >= ? AND start < ? ORDER BY start"
    with _conn() as conn:
        rows = _rows(conn, sql, (s, e))
    records = []
    for row in rows:
        rec = _strip_score_prefix(row)
        light = rec.get("total_light_sleep_time_milli")
        sws = rec.get("total_slow_wave_sleep_time_milli")
        rem = rec.get("total_rem_sleep_time_milli")
        if None not in (light, sws, rem):
            rec["total_sleep_milli"] = light + sws + rem
        else:
            rec["total_sleep_milli"] = None
        records.append(_add_local_times(rec))
    return {"count": len(records), "records": records}


@mcp.tool()
def get_cycles(start: str, end: str) -> dict:
    """Physiological cycles (day strain) starting in an inclusive date range.

    A WHOOP cycle is roughly one wake-to-wake period. Each record includes
    start/end, strain (0-21), kilojoule energy expenditure, average and max
    heart rate, and score_state.

    Use for questions about daily strain or energy expenditure.
    """
    s, e = _range(start, end)
    sql = "SELECT * FROM cycles WHERE start >= ? AND start < ? ORDER BY start"
    with _conn() as conn:
        rows = _rows(conn, sql, (s, e))
    records = [_add_local_times(_strip_score_prefix(r)) for r in rows]
    return {"count": len(records), "records": records}


@mcp.tool()
def get_workouts(start: str, end: str, sport: str | None = None) -> dict:
    """Workouts that started in an inclusive date range.

    Each record includes sport_name, start/end, strain, average/max heart rate,
    kilojoule, distance_meter, altitude change/gain, and time-in-zone (zone_*_milli).

    Optional `sport` filter: case-sensitive WHOOP sport name (e.g. 'WEIGHTLIFTING',
    'RUNNING', 'CYCLING'). To discover available sports, call without `sport` first.
    """
    s, e = _range(start, end)
    if sport:
        sql = "SELECT * FROM workouts WHERE start >= ? AND start < ? AND sport_name = ? ORDER BY start"
        params: tuple = (s, e, sport)
    else:
        sql = "SELECT * FROM workouts WHERE start >= ? AND start < ? ORDER BY start"
        params = (s, e)
    with _conn() as conn:
        rows = _rows(conn, sql, params)
    records = [_add_local_times(_strip_score_prefix(r)) for r in rows]
    return {"count": len(records), "records": records}


@mcp.tool()
def get_daily_summary(date: str) -> dict:
    """Recovery + main sleep + cycle (strain) + workouts for a single date (YYYY-MM-DD).

    'main sleep' is the longest non-nap sleep that ended on this date (the
    night-of preceding the morning). 'recovery' is the morning recovery
    associated with that sleep. 'cycle' is the cycle whose start is on this
    date. Any field may be null if the data is missing or still being scored.

    Use for 'how did I do on <day>' questions.
    """
    s, e = _day_range(date)
    with _conn() as conn:
        # Main night: non-nap sleep that ended on this date, longest first.
        sleep_row = conn.execute(
            """
            SELECT * FROM sleep
            WHERE nap = 0 AND end >= ? AND end < ?
            ORDER BY (
                COALESCE(score_total_light_sleep_time_milli, 0)
                + COALESCE(score_total_slow_wave_sleep_time_milli, 0)
                + COALESCE(score_total_rem_sleep_time_milli, 0)
            ) DESC
            LIMIT 1
            """,
            (s, e),
        ).fetchone()

        recovery = None
        sleep_clean = None
        if sleep_row is not None:
            sleep_clean = _add_local_times(_strip_score_prefix(sleep_row))
            light = sleep_clean.get("total_light_sleep_time_milli")
            sws = sleep_clean.get("total_slow_wave_sleep_time_milli")
            rem = sleep_clean.get("total_rem_sleep_time_milli")
            if None not in (light, sws, rem):
                sleep_clean["total_sleep_milli"] = light + sws + rem
            rec_row = conn.execute(
                "SELECT * FROM recovery WHERE sleep_id = ?",
                (sleep_row["id"],),
            ).fetchone()
            if rec_row is not None:
                recovery = _strip_score_prefix(rec_row)

        cycle_row = conn.execute(
            "SELECT * FROM cycles WHERE start >= ? AND start < ? ORDER BY start LIMIT 1",
            (s, e),
        ).fetchone()
        cycle = _add_local_times(_strip_score_prefix(cycle_row)) if cycle_row else None

        workout_rows = _rows(
            conn,
            "SELECT * FROM workouts WHERE start >= ? AND start < ? ORDER BY start",
            (s, e),
        )
    workouts = [_add_local_times(_strip_score_prefix(w)) for w in workout_rows]

    return {
        "date": date,
        "recovery": recovery,
        "sleep": sleep_clean,
        "cycle": cycle,
        "workouts": workouts,
    }


# ---------- aggregate tools ----------


def _period_averages(conn: sqlite3.Connection, start_iso: str, end_iso: str) -> dict[str, float | None]:
    """Average WHOOP metrics across a half-open date range."""
    rec = conn.execute(
        """
        SELECT AVG(r.score_recovery_score)     AS recovery_score,
               AVG(r.score_hrv_rmssd_milli)    AS hrv_rmssd_milli,
               AVG(r.score_resting_heart_rate) AS resting_heart_rate,
               COUNT(*)                        AS n_recovery
        FROM recovery r
        JOIN sleep s ON s.id = r.sleep_id
        WHERE s.start >= ? AND s.start < ?
        """,
        (start_iso, end_iso),
    ).fetchone()
    slp = conn.execute(
        """
        SELECT AVG(score_sleep_performance_percentage) AS sleep_performance_pct,
               AVG(score_sleep_efficiency_percentage)  AS sleep_efficiency_pct,
               AVG(
                   COALESCE(score_total_light_sleep_time_milli, 0)
                   + COALESCE(score_total_slow_wave_sleep_time_milli, 0)
                   + COALESCE(score_total_rem_sleep_time_milli, 0)
               ) AS total_sleep_milli,
               COUNT(*) AS n_sleep
        FROM sleep
        WHERE nap = 0 AND start >= ? AND start < ?
        """,
        (start_iso, end_iso),
    ).fetchone()
    cyc = conn.execute(
        "SELECT AVG(score_strain) AS strain, COUNT(*) AS n_cycles "
        "FROM cycles WHERE start >= ? AND start < ?",
        (start_iso, end_iso),
    ).fetchone()
    return {
        "recovery_score": rec["recovery_score"],
        "hrv_rmssd_milli": rec["hrv_rmssd_milli"],
        "resting_heart_rate": rec["resting_heart_rate"],
        "sleep_performance_pct": slp["sleep_performance_pct"],
        "sleep_efficiency_pct": slp["sleep_efficiency_pct"],
        "total_sleep_milli": slp["total_sleep_milli"],
        "strain": cyc["strain"],
        "n_recovery": rec["n_recovery"],
        "n_sleep_nights": slp["n_sleep"],
        "n_cycles": cyc["n_cycles"],
    }


@mcp.tool()
def compare_periods(start_a: str, end_a: str, start_b: str, end_b: str) -> dict:
    """Compare averaged metrics across two inclusive date ranges (e.g. this
    week vs last week). Each date is YYYY-MM-DD.

    Returns averages of recovery_score, hrv_rmssd_milli, resting_heart_rate,
    sleep_performance_pct, sleep_efficiency_pct, total_sleep_milli, and strain
    for each period, plus `delta` (period_b - period_a) per metric. Each period
    also reports sample counts (n_recovery, n_sleep_nights, n_cycles).

    Use for 'compare X vs Y' questions.
    """
    sa, ea = _range(start_a, end_a)
    sb, eb = _range(start_b, end_b)
    with _conn() as conn:
        a = _period_averages(conn, sa, ea)
        b = _period_averages(conn, sb, eb)
    metric_keys = [
        "recovery_score", "hrv_rmssd_milli", "resting_heart_rate",
        "sleep_performance_pct", "sleep_efficiency_pct",
        "total_sleep_milli", "strain",
    ]
    delta = {
        k: (b[k] - a[k]) if (a[k] is not None and b[k] is not None) else None
        for k in metric_keys
    }
    return {
        "period_a": {"start": start_a, "end": end_a, **a},
        "period_b": {"start": start_b, "end": end_b, **b},
        "delta": delta,
    }


# ---------- trends ----------


Metric = Literal[
    "recovery_score", "hrv", "resting_heart_rate",
    "sleep_performance", "sleep_efficiency", "strain",
]

# Each entry: (sql_template, value_column, date_column).
# {RANGE} is the half-open ISO range placeholder. The query must return columns
# named `day` (YYYY-MM-DD) and `value`.
_TREND_SQL: dict[str, str] = {
    "recovery_score": """
        SELECT substr(s.start, 1, 10) AS day, AVG(r.score_recovery_score) AS value
        FROM recovery r JOIN sleep s ON s.id = r.sleep_id
        WHERE s.start >= ? AND s.start < ?
        GROUP BY day ORDER BY day
    """,
    "hrv": """
        SELECT substr(s.start, 1, 10) AS day, AVG(r.score_hrv_rmssd_milli) AS value
        FROM recovery r JOIN sleep s ON s.id = r.sleep_id
        WHERE s.start >= ? AND s.start < ?
        GROUP BY day ORDER BY day
    """,
    "resting_heart_rate": """
        SELECT substr(s.start, 1, 10) AS day, AVG(r.score_resting_heart_rate) AS value
        FROM recovery r JOIN sleep s ON s.id = r.sleep_id
        WHERE s.start >= ? AND s.start < ?
        GROUP BY day ORDER BY day
    """,
    "sleep_performance": """
        SELECT substr(start, 1, 10) AS day, AVG(score_sleep_performance_percentage) AS value
        FROM sleep WHERE nap = 0 AND start >= ? AND start < ?
        GROUP BY day ORDER BY day
    """,
    "sleep_efficiency": """
        SELECT substr(start, 1, 10) AS day, AVG(score_sleep_efficiency_percentage) AS value
        FROM sleep WHERE nap = 0 AND start >= ? AND start < ?
        GROUP BY day ORDER BY day
    """,
    "strain": """
        SELECT substr(start, 1, 10) AS day, AVG(score_strain) AS value
        FROM cycles WHERE start >= ? AND start < ?
        GROUP BY day ORDER BY day
    """,
}


def _rolling(values: list[float | None], window: int) -> list[float | None]:
    """Right-aligned rolling mean. Window includes only non-null entries; if the
    window has no non-null values, the rolling output is null."""
    out: list[float | None] = []
    for i in range(len(values)):
        lo = max(0, i - window + 1)
        chunk = [v for v in values[lo : i + 1] if v is not None]
        out.append(sum(chunk) / len(chunk) if chunk else None)
    return out


@mcp.tool()
def get_trends(metric: Metric, window: int = 7, days: int = 90) -> dict:
    """Rolling-average trend for a daily metric over the last `days` days.

    `metric` is one of: recovery_score, hrv, resting_heart_rate,
    sleep_performance, sleep_efficiency, strain. `window` is the rolling-mean
    window in days (default 7). `days` is the lookback length (default 90).

    Returns {"metric", "window_days", "lookback_days", "series": [
        {"date": YYYY-MM-DD, "value": <daily>, "rolling": <rolling mean>}, ...]}.

    Use for 'how has my X trended' questions.
    """
    if window < 1:
        raise ValueError("window must be >= 1")
    if days < 1:
        raise ValueError("days must be >= 1")
    end_dt = datetime.now(tz=timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    start_dt = end_dt - timedelta(days=days)
    sql = _TREND_SQL[metric]
    with _conn() as conn:
        rows = _rows(conn, sql, (start_dt.isoformat(), end_dt.isoformat()))
    daily = [(r["day"], r["value"]) for r in rows]
    values = [v for _, v in daily]
    rolling = _rolling(values, window)
    series = [
        {"date": d, "value": v, "rolling": rolling[i]}
        for i, (d, v) in enumerate(daily)
    ]
    return {
        "metric": metric,
        "window_days": window,
        "lookback_days": days,
        "series": series,
    }


# ---------- the one tool that talks to the WHOOP API ----------


@mcp.tool()
def sync_whoop_data(since: str | None = None) -> dict:
    """Pull the latest WHOOP data into the local DB. Use BEFORE answering
    questions about today's or yesterday's data, or after a long gap.

    With no argument, runs a standard incremental sync (re-fetches the last
    couple of days to catch late-finalized scores). With `since='YYYY-MM-DD'`,
    runs a historical backfill from that date to now (slower; use to fill a
    gap rather than for routine refresh).

    Returns a dict mapping resource name to record count, e.g.
    {"cycles": 3, "sleep": 3, "workouts": 1, "recovery": 3}.
    """
    # The sync layer prints progress to stdout; stdout is reserved for JSON-RPC
    # under stdio transport, so redirect into stderr for the duration.
    with contextlib.redirect_stdout(sys.stderr):
        if since:
            start = _parse_date(since)
            return sync_mod.backfill(start, cfg=_cfg)
        return sync_mod.sync(cfg=_cfg)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
