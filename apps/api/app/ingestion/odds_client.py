"""The Odds API v4 client (odds ingestion for the MVP).

Official docs: https://the-odds-api.com/liveapi/guides/v4/

Endpoints used by the MVP (all GET, key passed as `apiKey` query param):

- ``GET /v4/sports/baseball_mlb/odds``
  Params: ``regions`` (e.g. "eu,us" — "eu" includes Pinnacle, the reference
  book), ``markets`` ("h2h" for moneyline, "h2h_1st_5_innings" for the
  First-5-Innings moneyline), ``oddsFormat=decimal``, ``bookmakers``
  (optional, filters to specific books and can reduce credit usage).
  Cost: 1 credit per region per market on the paid plans; the MVP budget is
  the ~20K credits tier, so the daily cron should batch markets in one call.

- ``GET /v4/sports/baseball_mlb/scores`` (Phase 2: grading picks)
  Params: ``daysFrom`` (1-3) for recently completed games.

This module is NOT called from tests: tests must never hit the network.
The API key comes from Settings (`ODDS_API_KEY` in `.env`).
"""

from typing import Any

import httpx

from app.config import Settings, get_settings

BASE_URL = "https://api.the-odds-api.com"

# Markets for the MVP: MLB moneyline + First-5-Innings moneyline.
MVP_MARKETS = ("h2h", "h2h_1st_5_innings")


class OddsApiError(RuntimeError):
    """Raised when The Odds API returns an error response."""


class OddsClient:
    """Thin httpx wrapper over The Odds API v4 for MLB odds."""

    def __init__(self, settings: Settings | None = None, timeout: float = 15.0) -> None:
        self._settings = settings or get_settings()
        self._timeout = timeout

    def get_mlb_odds(
        self,
        markets: tuple[str, ...] = MVP_MARKETS,
        regions: str = "eu,us",
        odds_format: str = "decimal",
        bookmakers: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch current MLB odds.

        Calls ``GET /v4/sports/baseball_mlb/odds`` with
        ``markets=h2h,h2h_1st_5_innings`` (comma-joined), ``regions`` and
        ``oddsFormat=decimal`` by default. Returns the parsed JSON list of
        events, each with `bookmakers -> markets -> outcomes` entries.

        Note on credits: each (region x market) combination consumes credits;
        prefer one batched call per scan over per-book calls. Remaining quota
        is reported in the `x-requests-remaining` response header.

        Raises:
            OddsApiError: on missing API key or non-2xx response.
        """
        if not self._settings.odds_api_key:
            raise OddsApiError("ODDS_API_KEY is not configured (see .env.example)")

        params: dict[str, str] = {
            "apiKey": self._settings.odds_api_key,
            "regions": regions,
            "markets": ",".join(markets),
            "oddsFormat": odds_format,
        }
        if bookmakers:
            params["bookmakers"] = bookmakers

        with httpx.Client(base_url=BASE_URL, timeout=self._timeout) as client:
            resp = client.get("/v4/sports/baseball_mlb/odds", params=params)
            if resp.status_code != 200:
                raise OddsApiError(
                    f"The Odds API returned {resp.status_code}: {resp.text[:500]}"
                )
            return resp.json()
