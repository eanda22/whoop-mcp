"""Typed models for WHOOP API v2 responses.

Field names mirror the API (snake_case). Score fields are nullable because
`score_state` can be PENDING_SCORE or UNSCORABLE — in which case the API
omits or nulls the score block. `extra="ignore"` so newly added WHOOP fields
do not break validation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict

ScoreState = Literal["SCORED", "PENDING_SCORE", "UNSCORABLE"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


# ---------- Cycle ----------


class CycleScore(_Model):
    strain: float
    kilojoule: float
    average_heart_rate: int
    max_heart_rate: int


class Cycle(_Model):
    id: int  # cycles kept integer IDs in v2; sleep/workout moved to UUIDs.
    user_id: int
    created_at: datetime
    updated_at: datetime
    start: datetime
    end: datetime | None = None  # open cycle while still in progress
    timezone_offset: str
    score_state: ScoreState
    score: CycleScore | None = None


# ---------- Recovery ----------


class RecoveryScore(_Model):
    user_calibrating: bool
    recovery_score: float
    resting_heart_rate: float
    hrv_rmssd_milli: float
    spo2_percentage: float | None = None
    skin_temp_celsius: float | None = None


class Recovery(_Model):
    cycle_id: int  # refers to Cycle.id (int)
    sleep_id: str  # UUID of associated sleep
    user_id: int
    created_at: datetime
    updated_at: datetime
    score_state: ScoreState
    score: RecoveryScore | None = None


# ---------- Sleep ----------


class SleepStageSummary(_Model):
    total_in_bed_time_milli: int
    total_awake_time_milli: int
    total_no_data_time_milli: int
    total_light_sleep_time_milli: int
    total_slow_wave_sleep_time_milli: int
    total_rem_sleep_time_milli: int
    sleep_cycle_count: int
    disturbance_count: int


class SleepNeeded(_Model):
    baseline_milli: int
    need_from_sleep_debt_milli: int
    need_from_recent_strain_milli: int
    need_from_recent_nap_milli: int


class SleepScore(_Model):
    stage_summary: SleepStageSummary
    sleep_needed: SleepNeeded
    respiratory_rate: float | None = None
    sleep_performance_percentage: float | None = None
    sleep_consistency_percentage: float | None = None
    sleep_efficiency_percentage: float | None = None


class Sleep(_Model):
    id: str  # UUID in v2
    v1_id: int | None = None  # legacy int id, present during transition
    cycle_id: int | None = None  # naps have no parent cycle
    user_id: int
    created_at: datetime
    updated_at: datetime
    start: datetime
    end: datetime
    timezone_offset: str
    nap: bool
    score_state: ScoreState
    score: SleepScore | None = None


# ---------- Workout ----------


class ZoneDurations(_Model):
    zone_zero_milli: int | None = None
    zone_one_milli: int | None = None
    zone_two_milli: int | None = None
    zone_three_milli: int | None = None
    zone_four_milli: int | None = None
    zone_five_milli: int | None = None


class WorkoutScore(_Model):
    strain: float
    average_heart_rate: int
    max_heart_rate: int
    kilojoule: float
    percent_recorded: float
    distance_meter: float | None = None
    altitude_gain_meter: float | None = None
    altitude_change_meter: float | None = None
    zone_durations: ZoneDurations | None = None


class Workout(_Model):
    id: str
    v1_id: int | None = None
    user_id: int
    created_at: datetime
    updated_at: datetime
    start: datetime
    end: datetime
    timezone_offset: str
    sport_name: str
    sport_id: int | None = None
    score_state: ScoreState
    score: WorkoutScore | None = None


# ---------- User ----------


class UserProfile(_Model):
    user_id: int
    email: str
    first_name: str
    last_name: str


class BodyMeasurement(_Model):
    height_meter: float
    weight_kilogram: float
    max_heart_rate: int


# ---------- Pagination envelope ----------

T = TypeVar("T", bound=BaseModel)


class Paginated(_Model, Generic[T]):
    records: list[T]
    next_token: str | None = None
