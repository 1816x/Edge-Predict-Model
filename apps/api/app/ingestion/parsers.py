"""Pure transformations from external API payloads to normalized rows.

These functions never touch the network or the database, so they are fully
unit-testable with recorded fixtures (see ``tests/fixtures/``). The store
layer (``app.ingestion.store``) is the only ingestion code that talks to
Postgres.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
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


@dataclass(frozen=True)
class BattingLine:
    """One batter's line in one game, from the boxscore endpoint.

    Only batters with a nonzero DERIVED plate-appearance count
    (AB + BB + HBP + SF + SH) are emitted: pinch runners and defensive
    substitutions contribute nothing to any rate feature. Counting events
    the feed omits when zero (doubles, triples, home runs, IBB, HBP, SF,
    SH) become true zeros; a missing required core stat (atBats, hits,
    strikeOuts, baseOnBalls) drops the line with an anomaly instead — a
    fabricated zero in a denominator would poison wOBA/K%/BB% silently.
    ``plate_appearances`` and ``batting_order`` are audit fields (None
    when absent), never feature denominators.
    """

    mlb_person_id: int
    full_name: str
    is_home: bool
    at_bats: int
    hits: int
    doubles: int
    triples: int
    home_runs: int
    walks: int
    intentional_walks: int
    strikeouts: int
    hit_by_pitch: int
    sac_flies: int
    sac_bunts: int
    batting_order: int | None
    plate_appearances: int | None


@dataclass(frozen=True)
class BoxscoreBatting:
    """All batting lines of one game plus anomalies and normal skips."""

    lines: tuple[BattingLine, ...]
    anomalies: tuple[str, ...]  # surfaced in job summaries, never silent
    zero_pa_skipped: int  # normal exclusions (pinch runners), not anomalies


def parse_boxscore_batting(payload: dict[str, Any]) -> BoxscoreBatting:
    """Normalize ``GET /api/v1/game/{gamePk}/boxscore`` batting lines.

    Rules:
    - Every player with a non-empty ``stats.batting`` block is considered
      (pitchers who batted included — their PAs were real offense).
    - Required: atBats, hits, strikeOuts, baseOnBalls; a line missing any
      is dropped with an anomaly (they feed every denominator).
    - Omission means zero for counting events: doubles, triples, homeRuns,
      intentionalWalks, hitByPitch, sacFlies, sacBunts.
    - Internally inconsistent lines (hits > AB, XBH > hits, IBB > BB) are
      dropped with an anomaly — the DB CHECKs would reject them anyway;
      better one loud line than a poisoned chunk.
    - Zero derived-PA lines are skipped and counted, not flagged.
    - A side whose batting section yields zero kept lines is an anomaly:
      that is exactly what a field-name drift in the feed would look
      like, and it must be loud, not silent.
    """
    lines: list[BattingLine] = []
    anomalies: list[str] = []
    zero_pa = 0

    for side_key, is_home in (("home", True), ("away", False)):
        side = (payload.get("teams") or {}).get(side_key) or {}
        considered = kept = 0
        for entry in (side.get("players") or {}).values():
            stats = ((entry.get("stats") or {}).get("batting")) or {}
            if not stats:
                continue
            person = entry.get("person") or {}
            pid = person.get("id")
            if pid is None:
                anomalies.append(f"{side_key}:batting_line_without_person_id")
                continue
            pid = int(pid)
            considered += 1
            required = {
                k: stats.get(k) for k in ("atBats", "hits", "strikeOuts", "baseOnBalls")
            }
            missing = [k for k, v in required.items() if v is None]
            if missing:
                anomalies.append(f"{side_key}:{pid}:batting_missing:{','.join(missing)}")
                continue
            _zero = lambda key: int(stats.get(key) or 0)  # noqa: E731
            at_bats, hits = int(stats["atBats"]), int(stats["hits"])
            walks, strikeouts = int(stats["baseOnBalls"]), int(stats["strikeOuts"])
            doubles, triples = _zero("doubles"), _zero("triples")
            home_runs = _zero("homeRuns")
            intentional_walks, hit_by_pitch = _zero("intentionalWalks"), _zero("hitByPitch")
            sac_flies, sac_bunts = _zero("sacFlies"), _zero("sacBunts")
            counting = (
                at_bats, hits, doubles, triples, home_runs, walks,
                intentional_walks, strikeouts, hit_by_pitch, sac_flies, sac_bunts,
            )
            # The negative guard runs BEFORE the zero-PA check on purpose: a
            # negative sac fly could zero out the derived PA and silently
            # reclassify a real line as a normal substitution. One negative
            # anywhere would also violate the table's CHECKs and roll back
            # the whole chunk's transaction — better one loud dropped line.
            if (
                any(v < 0 for v in counting)
                or hits > at_bats
                or doubles + triples + home_runs > hits
                or intentional_walks > walks
            ):
                anomalies.append(f"{side_key}:{pid}:batting_inconsistent")
                continue
            if at_bats + walks + hit_by_pitch + sac_flies + sac_bunts == 0:
                zero_pa += 1
                continue
            try:
                batting_order = int(entry.get("battingOrder"))
            except (TypeError, ValueError):
                batting_order = None
            if batting_order is not None and batting_order < 100:
                batting_order = None  # sub-100 slots are junk, not lineup data
            # Audit fields must never be able to kill the run: junk (non-
            # numeric or negative) plateAppearances degrades to None, same
            # treatment as battingOrder above.
            try:
                pa = int(stats.get("plateAppearances"))
            except (TypeError, ValueError):
                pa = None
            if pa is not None and pa < 0:
                pa = None
            kept += 1
            lines.append(
                BattingLine(
                    mlb_person_id=pid,
                    full_name=person.get("fullName") or f"MLB person {pid}",
                    is_home=is_home,
                    at_bats=at_bats,
                    hits=hits,
                    doubles=doubles,
                    triples=triples,
                    home_runs=home_runs,
                    walks=walks,
                    intentional_walks=intentional_walks,
                    strikeouts=strikeouts,
                    hit_by_pitch=hit_by_pitch,
                    sac_flies=sac_flies,
                    sac_bunts=sac_bunts,
                    batting_order=batting_order,
                    plate_appearances=pa,
                )
            )
        if considered and not kept:
            anomalies.append(f"{side_key}:no_batting_lines_kept")

    return BoxscoreBatting(
        lines=tuple(lines), anomalies=tuple(anomalies), zero_pa_skipped=zero_pa
    )


@dataclass(frozen=True)
class LineupSlot:
    """One STARTER's slot in a posted lineup (docs/04 §1.5).

    ``batting_order`` is MLB's hundreds encoding: 100 = leadoff starter,
    200 = 2-hole, ... 900 = 9th. Only starters (a multiple of 100) are
    emitted — mid-game subs (101, 201, ...) are not lineup data.
    """

    mlb_person_id: int
    full_name: str
    is_home: bool
    batting_order: int


@dataclass(frozen=True)
class BoxscoreLineup:
    """The posted starting lineups of one game plus parsing anomalies."""

    slots: tuple[LineupSlot, ...]
    anomalies: tuple[str, ...]  # surfaced in job summaries, never silent


def parse_boxscore_lineup(payload: dict[str, Any]) -> BoxscoreLineup:
    """Extract the posted STARTING lineups from a boxscore, stats-agnostic.

    Unlike ``parse_boxscore_batting`` (which needs a non-empty batting stat
    block and drops zero-PA lines), this reads the ``battingOrder`` field on
    each player entry directly — the lineup is posted ~1-4h before first
    pitch, when every batter still has zero plate appearances, so requiring
    stats would find nothing pre-game. Only starters (``battingOrder`` a
    multiple of 100) are returned.

    A side with zero starters is normal pre-game (lineup not posted yet), so
    it is NOT an anomaly. A PARTIAL side (1-8 starters, or a duplicated slot,
    or a slot without a person id) IS surfaced — that is what a field-name
    drift in the feed would look like, and it must be loud, not silent.
    """
    slots: list[LineupSlot] = []
    anomalies: list[str] = []

    for side_key, is_home in (("home", True), ("away", False)):
        side = (payload.get("teams") or {}).get(side_key) or {}
        seen: dict[int, int] = {}  # batting_order -> person id
        for entry in (side.get("players") or {}).values():
            try:
                order = int(entry.get("battingOrder"))
            except (TypeError, ValueError):
                continue
            if order < 100 or order % 100 != 0:
                continue  # subs (101, 201, ...) and junk are not starters
            person = entry.get("person") or {}
            pid = person.get("id")
            if pid is None:
                anomalies.append(f"{side_key}:lineup_slot_without_person_id:{order}")
                continue
            pid = int(pid)
            if order in seen:
                anomalies.append(f"{side_key}:duplicate_slot:{order}")
                continue
            seen[order] = pid
            slots.append(
                LineupSlot(
                    mlb_person_id=pid,
                    full_name=person.get("fullName") or f"MLB person {pid}",
                    is_home=is_home,
                    batting_order=order,
                )
            )
        if 0 < len(seen) < 9:
            anomalies.append(f"{side_key}:partial_lineup:{len(seen)}")

    return BoxscoreLineup(slots=tuple(slots), anomalies=tuple(anomalies))


@dataclass(frozen=True)
class PlayerTransaction:
    """One RAW player transaction from the MLB Stats API /transactions feed.

    Stored verbatim (docs/04 §1.5, migration 006): the IL classification
    (placement vs activation) is NOT decided here — it lives versioned in
    ``app/features/transactions.py`` over ``type_desc`` + ``description``. The
    parser only normalizes fields and surfaces drift as anomalies.

    ``transaction_date`` is the feed's ``date`` (the ANNOUNCE date, no time) —
    the as-of gate for backtest is ``transaction_date < event_day`` (<= t-1),
    so ``effectiveDate`` (which MLB retro-dates on IL placements) is
    deliberately IGNORED to avoid leaking an injury before it was public.
    ``mlb_transaction_id`` is the feed's stable natural key (the idempotency
    key); a row without one is dropped as an anomaly (it could not dedupe).
    """

    mlb_transaction_id: int
    mlb_person_id: int
    full_name: str
    from_team_mlb_id: int | None
    to_team_mlb_id: int | None
    type_code: str | None
    type_desc: str | None
    description: str | None
    transaction_date: date


@dataclass(frozen=True)
class TransactionsBatch:
    """The parsed transactions of one date-range call plus parsing anomalies."""

    rows: tuple[PlayerTransaction, ...]
    anomalies: tuple[str, ...]  # surfaced in job summaries, never silent


def _parse_date(value: str | None) -> date | None:
    """Parse a ``YYYY-MM-DD`` feed date. Tolerates a trailing time if present."""
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def parse_transactions(payload: dict[str, Any]) -> TransactionsBatch:
    """Normalize ``GET /api/v1/transactions`` into raw transaction rows.

    Stats-agnostic and taxonomy-agnostic: every transaction with a stable id,
    a person id and a parseable date is kept verbatim (type_code/type_desc/
    description untouched). Drops with a loud anomaly:
    - a transaction without ``id`` (the idempotency key — cannot dedupe),
    - a transaction without ``person.id`` (nothing to attribute the move to),
    - a transaction without a parseable ``date`` (the as-of gate).
    A ``person`` that is a team-level entry (no id) is such an anomaly, not a
    silent skip: that is what a field-name drift in the feed would look like.
    """
    rows: list[PlayerTransaction] = []
    anomalies: list[str] = []

    for entry in payload.get("transactions") or []:
        txn_id = entry.get("id")
        if txn_id is None:
            person = entry.get("person") or {}
            anomalies.append(
                f"txn_without_id:person={person.get('id')}:{entry.get('date')}"
            )
            continue
        txn_id = int(txn_id)
        person = entry.get("person") or {}
        pid = person.get("id")
        if pid is None:
            anomalies.append(f"txn_without_person_id:{txn_id}")
            continue
        pid = int(pid)
        tdate = _parse_date(entry.get("date"))
        if tdate is None:
            anomalies.append(f"txn_without_date:{txn_id}")
            continue
        from_team = entry.get("fromTeam") or {}
        to_team = entry.get("toTeam") or {}
        rows.append(
            PlayerTransaction(
                mlb_transaction_id=txn_id,
                mlb_person_id=pid,
                full_name=person.get("fullName") or f"MLB person {pid}",
                from_team_mlb_id=(
                    int(from_team["id"]) if from_team.get("id") is not None else None
                ),
                to_team_mlb_id=(
                    int(to_team["id"]) if to_team.get("id") is not None else None
                ),
                type_code=entry.get("typeCode"),
                type_desc=entry.get("typeDesc"),
                description=entry.get("description"),
                transaction_date=tdate,
            )
        )

    return TransactionsBatch(rows=tuple(rows), anomalies=tuple(anomalies))


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
