"""Shared transactions/IL math (docs/04 §1.5) for builder and dataset.

The transactions archive (migration 006, table ``player_transactions``) stores
RAW moves from the MLB Stats API /transactions feed. The IL classification and
the "on IL as-of date D" replay live HERE, versioned, so the online builder and
the bulk dataset cannot drift (the train/serve-skew bug class the parity test
guards) and a taxonomy change is a ``feature_version`` bump, not a re-backfill.

Decisions registered in docs/00-decisiones.md (2026-07-16, tanda F1.4):

- The as-of gate is DATE-based (the feed gives ``date`` without a time):
  ``transaction_date < event_day`` (UTC), i.e. a move dated on the game day is
  treated as unknown/not-yet-effective (<= t-1). Conservative and symmetric to
  the probable-vs-actual (§1.3) and confirmed-vs-realized (§1.5) biases; it also
  makes doubleheaders safe (both games share ``event_day``).
- ``il_effect`` classifies a move as +1 (player becomes unavailable) / -1
  (player returns) / None (not an IL move) by matching the raw text. It must
  recognize BOTH "injured list" (2019+) AND "disabled list" (the pre-2019 name
  for the same thing) — the backfill spans 2018, when the DL had not yet been
  renamed. A move that MENTIONS the IL/DL but matches no verb returns None and
  is surfaced by the ingest job's ``il_desc_unclassified`` drift canary.
"""

from __future__ import annotations

from app.features.lineup import batter_woba_asof
from app.features.offense import woba_parts

# star_out_flag (docs/04 §1.5): the count of a team's top-K batters who are on
# the IL as-of the game. "Top-K" is by trailing-year wOBA among established
# batters; the PA gate is measured by the wOBA denominator (at-bats + unintentional
# walks + SF + HBP), which both the online and bulk paths already compute, so no
# separate PA sum is needed and parity is free.
LINEUP_STAR_TOP_K = 2
LINEUP_STAR_MIN_PA = 200.0

# The status list has two historical names for the SAME thing: MLB renamed the
# Disabled List to the Injured List in 2019. The 2018 backfill sees "disabled
# list"; everything from 2019 on sees "injured list". Both must classify.
IL_STATUS_MARKERS = ("injured list", "disabled list")
# A move that puts / keeps a player OUT (a placement, or a transfer between IL
# tiers such as 10-day -> 60-day: the player stays out).
IL_PLACEMENT_MARKERS = ("placed", "transferred", "sent")
# A move that RETURNS a player to availability.
IL_ACTIVATION_MARKERS = ("activated", "reinstated")


def il_effect(
    type_code: str | None, type_desc: str | None, description: str | None
) -> int | None:
    """Classify a raw transaction: +1 out / -1 back / None not-an-IL-move.

    Matches the combined ``type_desc`` + ``description`` text case-insensitively.
    Activation is checked before placement so a description that names both
    (never seen, but be deterministic) resolves to the return. A row that
    mentions the IL/DL but matches no verb returns None on purpose — the ingest
    job flags those as ``il_desc_unclassified`` so feed drift is visible.
    """
    text = f"{type_desc or ''} {description or ''}".lower()
    if not any(marker in text for marker in IL_STATUS_MARKERS):
        return None
    if any(marker in text for marker in IL_ACTIVATION_MARKERS):
        return -1
    if any(marker in text for marker in IL_PLACEMENT_MARKERS):
        return 1
    return None


def mentions_il(type_desc: str | None, description: str | None) -> bool:
    """True if the raw text names the IL/DL (the ``il_desc_unclassified``
    universe): a mention that ``il_effect`` could not classify is feed drift."""
    text = f"{type_desc or ''} {description or ''}".lower()
    return any(marker in text for marker in IL_STATUS_MARKERS)


def il_out_asof(moves, event_day) -> bool:
    """Is a player on the IL as-of ``event_day`` (day-based, <= t-1)?

    ``moves`` is an iterable of ``(transaction_date, mlb_transaction_id, effect)``
    for ONE player, where ``effect`` is +1 (placed/kept out) or -1 (activated)
    from ``il_effect`` (non-IL moves already dropped). The state is the LATEST
    move strictly before ``event_day``: a move dated ON the game day is unknown
    (the feed carries no time, so <= t-1 is the conservative cut, and both games
    of a doubleheader share ``event_day``). Ties on the same date break by
    ``mlb_transaction_id`` — IDENTICALLY in the online and bulk paths, so a
    same-day place-then-activate resolves the same way in both. Empty (or no
    prior move) means available (False): never fabricate an injury.
    """
    asof = [m for m in moves if m[0] < event_day]
    if not asof:
        return False
    asof.sort(key=lambda m: (m[0], m[1]))
    return asof[-1][2] == 1


def top_k_star_players(
    player_sums: dict,
    k: int = LINEUP_STAR_TOP_K,
    min_pa: float = LINEUP_STAR_MIN_PA,
) -> list:
    """The team's top-K established batters by trailing-year wOBA, as-of.

    ``player_sums`` maps player_id -> that batter's 365d counting sums (already
    windowed to < event_day by the caller). A batter must clear ``min_pa``
    (measured by the wOBA denominator) to be an ESTABLISHED star — a thin
    small-sample line never counts. Ranked by ``batter_woba_asof`` (the same
    shrunk wOBA the lineup block uses), tie-broken by ``str(player_id)`` so the
    online and bulk paths pick the SAME players. Returns up to k player_ids
    (fewer, or empty, when the team has fewer qualifying batters as-of).
    """
    ranked = []
    for player_id, sums in player_sums.items():
        _, den = woba_parts(sums)
        if den < min_pa:
            continue
        woba = batter_woba_asof(sums)
        if woba is None:
            continue
        ranked.append((woba, str(player_id), player_id))
    ranked.sort(key=lambda r: (-r[0], r[1]))
    return [player_id for _, _, player_id in ranked[:k]]
