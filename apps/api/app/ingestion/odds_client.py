"""The Odds API v4 client (odds ingestion for the MVP).

Official docs: https://the-odds-api.com/liveapi/guides/v4/

The Odds API splits markets in two tiers, and they live on DIFFERENT
endpoints (mixing them returns 422 INVALID_MARKET):

- FEATURED markets (h2h, spreads, totals) come from the slate-wide endpoint
  ``GET /v4/sports/baseball_mlb/odds`` — one call returns every game.
  Cost: [unique markets returned] x [regions] per call.

- ADDITIONAL markets (e.g. ``h2h_1st_5_innings``, the F5 moneyline) are only
  served per event via ``GET /v4/sports/baseball_mlb/events/{id}/odds``.
  Cost: [markets] x [regions] PER EVENT with data (empty responses free).
  See docs/02-fuentes-de-datos.md for the credit plan.

Both endpoints return the same event shape (id, home_team, away_team,
commence_time, bookmakers[].markets[].outcomes[]), so one parser handles
both (``app.ingestion.parsers.parse_odds_event``).

This module is NOT called from tests: tests must never hit the network.
The API key comes from Settings (`ODDS_API_KEY` in `.env`).
"""

from typing import Any

import httpx

from app.config import Settings, get_settings

BASE_URL = "https://api.the-odds-api.com"

# Featured market for the MVP: MLB moneyline. Slate-wide endpoint only.
FEATURED_MARKETS = ("h2h",)

# Additional market for the MVP: First-5-Innings moneyline. Per-event only.
F5_MARKET = "h2h_1st_5_innings"


class OddsApiError(RuntimeError):
    """Raised when The Odds API returns an error response."""


class OddsClient:
    """Thin httpx wrapper over The Odds API v4 for MLB odds."""

    def __init__(self, settings: Settings | None = None, timeout: float = 15.0) -> None:
        self._settings = settings or get_settings()
        self._timeout = timeout

    def _get(self, path: str, params: dict[str, str]) -> Any:
        if not self._settings.odds_api_key:
            raise OddsApiError("ODDS_API_KEY is not configured (see .env.example)")
        params = {"apiKey": self._settings.odds_api_key, **params}
        with httpx.Client(base_url=BASE_URL, timeout=self._timeout) as client:
            resp = client.get(path, params=params)
            if resp.status_code != 200:
                raise OddsApiError(
                    f"The Odds API returned {resp.status_code}: {resp.text[:500]}"
                )
            return resp.json()

    def get_mlb_odds(
        self,
        markets: tuple[str, ...] = FEATURED_MARKETS,
        regions: str = "eu,us",
        odds_format: str = "decimal",
        bookmakers: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch current MLB odds for FEATURED markets (whole slate, one call).

        Do NOT pass additional markets here (e.g. h2h_1st_5_innings): the
        endpoint rejects them with 422 INVALID_MARKET. Use
        :meth:`get_event_odds` for those.

        Remaining quota is reported in the `x-requests-remaining` header.

        Raises:
            OddsApiError: on missing API key or non-2xx response.
        """
        params: dict[str, str] = {
            "regions": regions,
            "markets": ",".join(markets),
            "oddsFormat": odds_format,
        }
        if bookmakers:
            params["bookmakers"] = bookmakers
        return self._get("/v4/sports/baseball_mlb/odds", params)

    def get_event_odds(
        self,
        event_id: str,
        markets: tuple[str, ...] = (F5_MARKET,),
        regions: str = "eu,us",
        odds_format: str = "decimal",
        bookmakers: str | None = None,
    ) -> dict[str, Any]:
        """Fetch one event's odds — the only endpoint serving ADDITIONAL
        markets like the F5 moneyline.

        Returns a single event object (same shape as the slate endpoint's
        entries). Books without the requested market simply don't appear.

        Raises:
            OddsApiError: on missing API key or non-2xx response.
        """
        params: dict[str, str] = {
            "regions": regions,
            "markets": ",".join(markets),
            "oddsFormat": odds_format,
        }
        if bookmakers:
            params["bookmakers"] = bookmakers
        return self._get(f"/v4/sports/baseball_mlb/events/{event_id}/odds", params)
