"""Pure transformations from external API payloads to normalized rows.

These functions never touch the network or the database, so they are fully
unit-testable with recorded fixtures (see ``tests/fixtures/``). The store
layer (``app.ingestion.store``) is the only ingestion code that talks to
Postgres.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.core.devig import decimal_to_american

# The Odds API market key -> our market_code enum (infra/schema.sql).
# Anything not listed here (spreads, totals, ...) is skipped until phase 2.
ODDS_API_MARKET_MAP = {
    "h2h": "moneyline",
    "h2h_1st_5_innings": "f5_moneyline",
}

# MLB Stats API abstractGameState -> our event_status enum. Postponed and
# cancelled games carry it in detailedState instead, handled separately.
_MLB_ABSTRACT_STATUS_MAP = {
    "Preview": "scheduled",
    "Live": "live",
    "Final": "final",
}

# numeric(8, 3) storage rounds prices to 3 decimals; anything at or below
# 1.001 could round into the CHECK (price_decimal > 1.0) and is junk anyway.
_MIN_DECIMAL_PRICE = 1.001


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


@dataclass(frozen=True)
class OddsOutcome:
    """One (book, market, side) price from a bookmaker payload."""

    book_key: str
    market: str  # market_code enum value
    side: str  # outcome_side enum value: 'home' | 'away'
    price_decimal: float
    price_american: int
    last_update: datetime | None


@dataclass(frozen=True)
class OddsEvent:
    """Normalized The Odds API event with its parseable outcomes."""

    source_id: str
    home_team: str
    away_team: str
    commence_time: datetime
    outcomes: tuple[OddsOutcome, ...]
    skipped: tuple[str, ...]  # unparseable entries, surfaced in job summaries


def parse_odds_event(payload: dict[str, Any]) -> OddsEvent:
    """Normalize one event from ``GET /v4/sports/baseball_mlb/odds``.

    Rules:
    - Only markets in ODDS_API_MARKET_MAP are kept (e.g. ``spreads`` skips).
    - An outcome must name the home or away team; anything else (e.g. a
      ``Draw`` leg in three-way F5 listings) is skipped and reported.
    - Prices must be valid decimal odds (> 1.001, see _MIN_DECIMAL_PRICE).
    """
    home = payload["home_team"]
    away = payload["away_team"]
    outcomes: list[OddsOutcome] = []
    skipped: list[str] = []

    for bookmaker in payload.get("bookmakers", []):
        book_key = bookmaker["key"]
        for market_obj in bookmaker.get("markets", []):
            market = ODDS_API_MARKET_MAP.get(market_obj["key"])
            if market is None:
                skipped.append(f"{book_key}:market:{market_obj['key']}")
                continue
            last_update = _parse_ts(market_obj.get("last_update"))
            for outcome in market_obj.get("outcomes", []):
                name = outcome["name"]
                if name == home:
                    side = "home"
                elif name == away:
                    side = "away"
                else:
                    skipped.append(f"{book_key}:{market}:outcome:{name}")
                    continue
                price = float(outcome["price"])
                if price <= _MIN_DECIMAL_PRICE:
                    skipped.append(f"{book_key}:{market}:{side}:price:{price}")
                    continue
                outcomes.append(
                    OddsOutcome(
                        book_key=book_key,
                        market=market,
                        side=side,
                        price_decimal=price,
                        price_american=int(round(decimal_to_american(price))),
                        last_update=last_update,
                    )
                )

    return OddsEvent(
        source_id=payload["id"],
        home_team=home,
        away_team=away,
        commence_time=_parse_ts(payload["commence_time"]),
        outcomes=tuple(outcomes),
        skipped=tuple(skipped),
    )


@dataclass(frozen=True)
class ScheduledGame:
    """Normalized game from the MLB Stats API daily schedule."""

    game_pk: int
    start_time: datetime
    status: str  # event_status enum value
    home_name: str
    away_name: str
    home_mlb_id: int | None
    away_mlb_id: int | None
    home_probable: str | None
    away_probable: str | None
    # MLB gameType: 'R' regular season, 'S' spring training, 'E' exhibition,
    # 'F'/'D'/'L'/'W' postseason rounds, 'A' all-star. None if absent.
    game_type: str | None = None


@dataclass(frozen=True)
class GameResult:
    """Final score of a game, with First-5-Innings partials when derivable.

    F5 scores are the sum of runs in innings 1-5 from the linescore. They are
    None unless BOTH sides have runs recorded for all of the first five
    innings — rain-shortened or oddly-terminated games settle manually rather
    than with a silently wrong number (docs/06, honest settlement).
    """

    game_pk: int
    home_score: int
    away_score: int
    f5_home_score: int | None
    f5_away_score: int | None


def _f5_sums(linescore: dict[str, Any]) -> tuple[int | None, int | None]:
    innings = linescore.get("innings", [])
    if len(innings) < 5:
        return None, None
    home_runs: list[int] = []
    away_runs: list[int] = []
    for inning in innings[:5]:
        home = (inning.get("home") or {}).get("runs")
        away = (inning.get("away") or {}).get("runs")
        if home is None or away is None:
            return None, None
        home_runs.append(int(home))
        away_runs.append(int(away))
    return sum(home_runs), sum(away_runs)


def parse_schedule_results(payload: dict[str, Any]) -> list[GameResult]:
    """Extract final scores from ``GET /api/v1/schedule?hydrate=linescore``.

    Only games whose ``abstractGameState`` is ``Final`` AND that carry both
    team scores are returned; postponed/cancelled/suspended games are the
    schedule sync's problem, not the results backfill's.
    """
    results: list[GameResult] = []
    for day in payload.get("dates", []):
        for game in day.get("games", []):
            status_obj = game.get("status", {})
            if status_obj.get("abstractGameState") != "Final":
                continue
            if status_obj.get("detailedState") in ("Postponed", "Cancelled"):
                continue
            home = game["teams"]["home"]
            away = game["teams"]["away"]
            if home.get("score") is None or away.get("score") is None:
                continue
            f5_home, f5_away = _f5_sums(game.get("linescore") or {})
            results.append(
                GameResult(
                    game_pk=int(game["gamePk"]),
                    home_score=int(home["score"]),
                    away_score=int(away["score"]),
                    f5_home_score=f5_home,
                    f5_away_score=f5_away,
                )
            )
    return results


def parse_schedule(payload: dict[str, Any]) -> list[ScheduledGame]:
    """Normalize ``GET /api/v1/schedule?hydrate=probablePitcher`` output."""
    games: list[ScheduledGame] = []
    for day in payload.get("dates", []):
        for game in day.get("games", []):
            status_obj = game.get("status", {})
            detailed = status_obj.get("detailedState", "")
            if detailed == "Postponed":
                status = "postponed"
            elif detailed == "Cancelled":
                status = "cancelled"
            else:
                status = _MLB_ABSTRACT_STATUS_MAP.get(
                    status_obj.get("abstractGameState", ""), "scheduled"
                )
            home = game["teams"]["home"]
            away = game["teams"]["away"]
            games.append(
                ScheduledGame(
                    game_pk=int(game["gamePk"]),
                    start_time=_parse_ts(game["gameDate"]),
                    status=status,
                    home_name=home["team"]["name"],
                    away_name=away["team"]["name"],
                    home_mlb_id=home["team"].get("id"),
                    away_mlb_id=away["team"].get("id"),
                    home_probable=(home.get("probablePitcher") or {}).get("fullName"),
                    away_probable=(away.get("probablePitcher") or {}).get("fullName"),
                    game_type=game.get("gameType"),
                )
            )
    return games
