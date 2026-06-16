"""WHOOP API v2 client.

A thin, typed sync wrapper over the WHOOP REST API. Handles bearer-token
injection, transparent refresh on 401 (delegating to `auth.refresh`, which
already persists the rotated refresh token), and nextToken-based pagination
to walk full date ranges.

Smoke run:
    uv run python -m whoop_mcp.client
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import httpx

from . import auth
from .config import Config, load_config
from .models import (
    BodyMeasurement,
    Cycle,
    Recovery,
    Sleep,
    UserProfile,
    Workout,
)

BASE_URL = "https://api.prod.whoop.com/developer"
PAGE_LIMIT = 25  # WHOOP caps collection responses at 25 records per page.


def _iso(dt: datetime) -> str:
    """ISO 8601 in UTC with 'Z'. WHOOP requires aware UTC timestamps."""
    if dt.tzinfo is None:
        raise ValueError("Datetime must be timezone-aware (UTC).")
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class WhoopClient:
    """Typed client over the WHOOP v2 API. One per process is enough."""

    def __init__(
        self,
        cfg: Config | None = None,
        *,
        http: httpx.Client | None = None,
    ) -> None:
        self.cfg = cfg or load_config()
        self._http = http or httpx.Client(base_url=BASE_URL, timeout=30.0)
        self._tokens = auth.get_valid_tokens(self.cfg)

    # ----- internals -----

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._tokens.access_token}"}

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        response = self._http.request(method, path, params=params, headers=self._headers())
        if response.status_code == 401:
            # Force a refresh and retry once. `auth.refresh` persists the
            # rotated refresh token to disk.
            self._tokens = auth.refresh(self._tokens, self.cfg)
            response = self._http.request(
                method, path, params=params, headers=self._headers()
            )
        response.raise_for_status()
        return response

    def _get_json(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request("GET", path, params=params).json()

    def _paginate(
        self,
        path: str,
        *,
        start: datetime,
        end: datetime,
    ) -> Iterator[dict[str, Any]]:
        next_token: str | None = None
        while True:
            params: dict[str, Any] = {
                "limit": PAGE_LIMIT,
                "start": _iso(start),
                "end": _iso(end),
            }
            if next_token:
                params["nextToken"] = next_token
            body = self._get_json(path, params=params)
            for record in body.get("records", []):
                yield record
            next_token = body.get("next_token")
            if not next_token:
                return

    # ----- cycles -----

    def get_cycles(self, start: datetime, end: datetime) -> list[Cycle]:
        return [Cycle.model_validate(r) for r in self._paginate("/v2/cycle", start=start, end=end)]

    def get_cycle(self, cycle_id: int) -> Cycle:
        return Cycle.model_validate(self._get_json(f"/v2/cycle/{cycle_id}"))

    def get_sleep_for_cycle(self, cycle_id: int) -> Sleep:
        return Sleep.model_validate(self._get_json(f"/v2/cycle/{cycle_id}/sleep"))

    # ----- recovery -----

    def get_recovery(self, start: datetime, end: datetime) -> list[Recovery]:
        return [
            Recovery.model_validate(r)
            for r in self._paginate("/v2/recovery", start=start, end=end)
        ]

    def get_recovery_for_cycle(self, cycle_id: int) -> Recovery | None:
        """Returns None when WHOOP has no recovery for this cycle yet.

        Recovery is a morning metric — a 404 before wake is expected, not an
        error. All other HTTP errors propagate.
        """
        try:
            data = self._get_json(f"/v2/cycle/{cycle_id}/recovery")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise
        return Recovery.model_validate(data)

    # ----- sleep -----

    def get_sleep(self, start: datetime, end: datetime) -> list[Sleep]:
        return [
            Sleep.model_validate(r)
            for r in self._paginate("/v2/activity/sleep", start=start, end=end)
        ]

    def get_sleep_by_id(self, sleep_id: str) -> Sleep:
        return Sleep.model_validate(self._get_json(f"/v2/activity/sleep/{sleep_id}"))

    # ----- workouts -----

    def get_workouts(self, start: datetime, end: datetime) -> list[Workout]:
        return [
            Workout.model_validate(r)
            for r in self._paginate("/v2/activity/workout", start=start, end=end)
        ]

    def get_workout_by_id(self, workout_id: str) -> Workout:
        return Workout.model_validate(
            self._get_json(f"/v2/activity/workout/{workout_id}")
        )

    # ----- user -----

    def get_user_profile(self) -> UserProfile:
        return UserProfile.model_validate(self._get_json("/v2/user/profile/basic"))

    def get_body_measurement(self) -> BodyMeasurement:
        return BodyMeasurement.model_validate(self._get_json("/v2/user/measurement/body"))

    # ----- lifecycle -----

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "WhoopClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


# ---------- smoke entrypoint ----------


def _smoke(days: int) -> None:
    end = datetime.now(tz=timezone.utc)
    start = end - timedelta(days=days)

    with WhoopClient() as client:
        profile = client.get_user_profile()
        print(f"profile: {profile.first_name} {profile.last_name} (user_id={profile.user_id})")

        cycles = client.get_cycles(start, end)
        print(f"cycles  ({days}d): {len(cycles)}")

        recoveries = client.get_recovery(start, end)
        print(f"recovery({days}d): {len(recoveries)}")

        workouts = client.get_workouts(start, end)
        print(f"workouts({days}d): {len(workouts)}")

        body = client.get_body_measurement()
        print(
            f"body: {body.height_meter}m, {body.weight_kilogram}kg, max_hr={body.max_heart_rate}"
        )

    print("OK: models validated; pagination completed without error.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="whoop-client", description=__doc__)
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Lookback window in days for the smoke run (default: 30).",
    )
    args = parser.parse_args()
    _smoke(args.days)


if __name__ == "__main__":
    main()
