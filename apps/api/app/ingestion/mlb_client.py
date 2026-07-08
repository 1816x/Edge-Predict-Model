"""MLB Stats API client (free, no key required).

Base URL: https://statsapi.mlb.com

Endpoints used by the MVP (documented informally by the community; MLB has
no official public docs page, see https://github.com/toddrob99/MLB-StatsAPI
for a maintained wrapper reference):

- ``GET /api/v1/schedule?sportId=1&date=YYYY-MM-DD&hydrate=probablePitcher``
  Daily slate with gamePk ids, teams, venue, game status and probable
  starting pitchers (the `hydrate=probablePitcher` expansion). This feeds
  the daily cron scan and the as-of feature builder.

- ``GET /api/v1/game/{gamePk}/boxscore``
  Final boxscore per game: batting/pitching lines per team and player,
  including inning-by-inning linescore data needed to grade First-5-Innings
  (F5) markets. Used after games finish to grade picks.

Terms of use: statsapi.mlb.com is free for individual, non-commercial-scale
use; heavy commercial redistribution of raw MLB data may require a license.
Keep request volume low (daily slate + finished games only).

This module is NOT called from tests: tests must never hit the network.
"""

from typing import Any

import httpx

BASE_URL = "https://statsapi.mlb.com"


class MlbApiError(RuntimeError):
    """Raised when the MLB Stats API returns an error response."""


class MlbClient:
    """Thin httpx wrapper over the MLB Stats API."""

    def __init__(self, timeout: float = 15.0) -> None:
        self._timeout = timeout

    def get_schedule(self, date_iso: str) -> dict[str, Any]:
        """Daily schedule with probable pitchers.

        Calls ``GET /api/v1/schedule?sportId=1&date={date_iso}&hydrate=probablePitcher``.

        Args:
            date_iso: date in ``YYYY-MM-DD`` format.

        Returns:
            Parsed JSON with `dates[].games[]`, each carrying `gamePk`,
            `teams.home/away` (including `probablePitcher` when announced),
            `venue` and `status`.

        Raises:
            MlbApiError: on non-2xx response.
        """
        params = {"sportId": "1", "date": date_iso, "hydrate": "probablePitcher"}
        with httpx.Client(base_url=BASE_URL, timeout=self._timeout) as client:
            resp = client.get("/api/v1/schedule", params=params)
            if resp.status_code != 200:
                raise MlbApiError(f"MLB Stats API returned {resp.status_code}: {resp.text[:500]}")
            return resp.json()

    def get_boxscore(self, game_pk: int) -> dict[str, Any]:
        """Boxscore for a finished (or live) game.

        Calls ``GET /api/v1/game/{gamePk}/boxscore``. Used to grade picks
        (full-game and F5 results) after games finish.

        Raises:
            MlbApiError: on non-2xx response.
        """
        with httpx.Client(base_url=BASE_URL, timeout=self._timeout) as client:
            resp = client.get(f"/api/v1/game/{game_pk}/boxscore")
            if resp.status_code != 200:
                raise MlbApiError(f"MLB Stats API returned {resp.status_code}: {resp.text[:500]}")
            return resp.json()
