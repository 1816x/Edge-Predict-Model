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
    # MLB person ids of the probable pitchers — the durable identity that
    # event_probables stores (names collide and change; ids do not).
    home_probable_id: int | None = None
    away_probable_id: int | None = None


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
                    home_probable_id=(home.get("probablePitcher") or {}).get("id"),
                    away_probable_id=(away.get("probablePitcher") or {}).get("id"),
                )
            )
    return games


@dataclass(frozen=True)
class PitchingLine:
    """One pitcher's line in one game, from the boxscore endpoint.

    ``outs_recorded`` is innings pitched as exact outs (5.2 IP -> 17); the
    fractional-IP float never exists in this codebase. Columns that old
    boxscores sometimes omit (fly_outs, ground_outs, sac_flies,
    pitches_thrown) are None rather than fabricated zeros — the xFIP
    denominator must know the difference between "no fly balls" and
    "not recorded".
    """

    mlb_person_id: int
    full_name: str
    pitch_hand: str | None  # 'L' | 'R' | 'S'; boxscores usually omit it
    is_home: bool
    is_starter: bool
    outs_recorded: int
    batters_faced: int
    strikeouts: int
    walks: int
    hit_batsmen: int
    home_runs: int
    fly_outs: int | None
    ground_outs: int | None
    sac_flies: int | None
    pitches_thrown: int | None


@dataclass(frozen=True)
class BoxscorePitching:
    """All pitching lines of one game plus any parsing anomalies."""

    lines: tuple[PitchingLine, ...]
    anomalies: tuple[str, ...]  # surfaced in job summaries, never silent


def _outs_from_stats(stats: dict[str, Any]) -> int | None:
    """Outs recorded: the ``outs`` stat, else parsed from ``inningsPitched``.

    MLB writes innings pitched as "5.2" meaning 5 innings and 2 outs — NOT
    a decimal number. Parsing it as float would corrupt every rate stat.
    """
    outs = stats.get("outs")
    if outs is not None:
        return int(outs)
    ip = stats.get("inningsPitched")
    if ip is None:
        return None
    whole, _, frac = str(ip).partition(".")
    return int(whole or 0) * 3 + (int(frac[0]) if frac else 0)


def parse_boxscore_pitching(payload: dict[str, Any]) -> BoxscorePitching:
    """Normalize ``GET /api/v1/game/{gamePk}/boxscore`` pitching lines.

    Rules:
    - Every player with a non-empty ``stats.pitching`` block counts (position
      players who pitched included — they faced real batters).
    - The starter is the pitcher with ``gamesStarted >= 1``; if the flag is
      missing on every line (old data), fall back to the first id in the
      team's ``pitchers`` appearance-order list. Zero or 2+ starters on a
      side is reported as an anomaly for the job summary.
    - A line missing outs, battersFaced, strikeOuts or baseOnBalls is
      dropped with an anomaly: those are the feature denominators and a
      fabricated zero would poison K-BB% silently. hitBatsmen/homeRuns
      default to 0 (omission means none); fly/ground/sac/pitches stay None.
    """
    lines: list[PitchingLine] = []
    anomalies: list[str] = []

    for side_key, is_home in (("home", True), ("away", False)):
        side = (payload.get("teams") or {}).get(side_key) or {}
        appearance_order = [int(pid) for pid in side.get("pitchers") or []]
        raw: dict[int, dict[str, Any]] = {}
        for entry in (side.get("players") or {}).values():
            stats = ((entry.get("stats") or {}).get("pitching")) or {}
            if not stats:
                continue
            person = entry.get("person") or {}
            pid = person.get("id")
            if pid is None:
                anomalies.append(f"{side_key}:pitching_line_without_person_id")
                continue
            raw[int(pid)] = {"person": person, "stats": stats}

        starters = [pid for pid, r in raw.items() if (r["stats"].get("gamesStarted") or 0) >= 1]
        if not starters and raw:
            # Only the FIRST pitcher in appearance order can be the starter.
            # If his line didn't parse, crowning the next one would flag a
            # reliever as starter — worse than reporting no starter at all.
            fallback = appearance_order[0] if appearance_order else None
            if fallback is not None and fallback in raw:
                starters = [fallback]
                anomalies.append(f"{side_key}:starter_from_appearance_order:{fallback}")
        if len(starters) != 1:
            anomalies.append(f"{side_key}:starter_count:{len(starters)}")
        if len(starters) > 1:
            # Keep the earliest in appearance order; demote the rest.
            starters.sort(
                key=lambda pid: appearance_order.index(pid)
                if pid in appearance_order
                else len(appearance_order)
            )
            starters = starters[:1]
        starter_id = starters[0] if starters else None

        for pid, r in raw.items():
            stats = r["stats"]
            outs = _outs_from_stats(stats)
            required = {
                "outs": outs,
                "battersFaced": stats.get("battersFaced"),
                "strikeOuts": stats.get("strikeOuts"),
                "baseOnBalls": stats.get("baseOnBalls"),
            }
            missing = [k for k, v in required.items() if v is None]
            if missing:
                anomalies.append(f"{side_key}:{pid}:missing:{','.join(missing)}")
                continue
            hand = ((r["person"].get("pitchHand") or {}).get("code")) or None
            _opt = lambda key: None if stats.get(key) is None else int(stats[key])  # noqa: E731
            lines.append(
                PitchingLine(
                    mlb_person_id=pid,
                    full_name=r["person"].get("fullName") or f"MLB person {pid}",
                    pitch_hand=hand if hand in ("L", "R", "S") else None,
                    is_home=is_home,
                    is_starter=pid == starter_id,
                    outs_recorded=int(outs),
                    batters_faced=int(stats["battersFaced"]),
                    strikeouts=int(stats["strikeOuts"]),
                    walks=int(stats["baseOnBalls"]),
                    hit_batsmen=int(stats.get("hitBatsmen") or 0),
                    home_runs=int(stats.get("homeRuns") or 0),
                    fly_outs=_opt("flyOuts"),
                    ground_outs=_opt("groundOuts"),
                    sac_flies=_opt("sacFlies"),
                    pitches_thrown=_opt("numberOfPitches"),
                )
            )

    return BoxscorePitching(lines=tuple(lines), anomalies=tuple(anomalies))
