"""Shared lineup-block math (docs/04 §1.5) for builder and dataset.

The lineup block turns the team-aggregate offense of §1.2 into a
per-batter signal weighted by the real batting order — the payoff the
F1.2 offense block measured as marginal at team level (train_f1 v4). Like
``offense.py``, the FORMULAS live here once so the online builder and the
bulk dataset cannot drift (the train/serve-skew bug class the parity test
guards); only the WINDOW/lineup-composition semantics live in each path.

Per-batter wOBA reuses ``offense.woba_parts`` on a single batter's windowed
sums (wOBA is already defined over a dict of counting sums). Decisions
registered in docs/00-decisiones.md (2026-07-15, tanda F1.3):

- Window = trailing 365 UTC days ending yesterday (D2): a 30-day batter
  window is ~100 PA and dominated by noise; 365d mirrors
  ``OFFENSE_SPLIT_TARGET_DAYS`` and the split-target philosophy.
- Shrinkage toward a FROZEN league wOBA prior (D3): each batter's 365d
  wOBA is pulled toward LINEUP_BATTER_WOBA_PRIOR (a pre-2018 MLB average,
  leak-free by construction exactly like the FanGraphs 2017 wOBA weights)
  with LINEUP_BATTER_SHRINK_PA pseudo-PA. A batter with ZERO trailing-year
  PA is dropped from the projection (None), never injected with the
  prior — injecting the league average would fabricate a batter into the
  lineup. The top-4 vs-hand split shrinks toward the batter's OWN overall
  365d wOBA (platoon shrink) with the smaller LINEUP_SPLIT_SHRINK_PA.
- Order weighting = fixed PA-share by slot (D1): leadoff sees more PAs
  than the 9-hole. The vector is a frozen as-of-safe constant (2017 PA per
  lineup slot), so it never needs as-of machinery. Incomplete lineups and
  batters without a usable wOBA renormalize over the present slots (D5).
"""

from __future__ import annotations

from app.features.offense import woba_parts

# D2 — trailing-year batter window (same horizon as the offense split target).
LINEUP_BATTER_WINDOW_DAYS = 365

# D3 — frozen league wOBA prior (~2017 MLB average, pre-dataset hence
# leak-free) and the pseudo-PA that pulls a thin 365d batter sample toward
# it. A batter with no trailing-year PA is dropped, not shrunk to the prior.
LINEUP_BATTER_WOBA_PRIOR = 0.320
LINEUP_BATTER_SHRINK_PA = 100.0
# Platoon split shrink: the top-4 same-hand wOBA is pulled toward the
# batter's OWN overall 365d wOBA. Small pseudo-PA because a single batter's
# same-hand sample is tiny and we do not want to swamp it (offense.py's
# TEAM_SPLIT_SHRINK_PA=200 is a team-level constant that would drown a
# batter, so shrunk_split is deliberately NOT reused here).
LINEUP_SPLIT_SHRINK_PA = 50.0

# D1 — fixed PA-share by lineup slot (1..9). Empirical MLB PA per lineup
# slot per game from a pre-dataset reference season (2017): the leadoff
# hitter bats ~4.65 times, the 9-hole ~3.85. Frozen and normalized to sum
# 1.0; cited as an as-of-safe constant like the FanGraphs 2017 wOBA weights.
# A model feature only cares about ordering and stability, not the absolute
# scale, so freezing keeps every row on one basis for free.
LINEUP_PA_SHARE = (
    0.1216, 0.1190, 0.1163, 0.1137, 0.1110, 0.1085, 0.1059, 0.1033, 0.1007,
)
TOP4_SLOTS = 4

LINEUP_FEATURE_NAMES = (
    "lineup_is_confirmed",
    "lineup_woba_proj",
    "top4_woba_vs_hand",
)


def batter_woba_asof(sums: dict) -> float | None:
    """One batter's trailing-year wOBA, shrunk toward the league prior.

    ``sums`` are the batter's counting sums over the 365d window. With no
    plate appearances (denominator <= 0) the batter is dropped (None) —
    the league prior is NEVER injected as if it were this batter's line.
    """
    num, den = woba_parts(sums)
    if den <= 0:
        return None
    return round(
        (num + LINEUP_BATTER_SHRINK_PA * LINEUP_BATTER_WOBA_PRIOR)
        / (den + LINEUP_BATTER_SHRINK_PA),
        4,
    )


def batter_woba_vs_hand_asof(sums_hand: dict, sums_overall: dict) -> float | None:
    """One batter's same-hand wOBA, shrunk toward their OWN overall wOBA.

    ``sums_hand`` are the batter's sums vs the opposing starter's hand;
    ``sums_overall`` their overall 365d sums (the shrink target). With no
    overall sample (denominator <= 0) the batter is dropped (None). An
    empty same-hand window yields the pure prior (the batter's overall
    wOBA) — never a fabricated zero.
    """
    num_o, den_o = woba_parts(sums_overall)
    if den_o <= 0:
        return None
    target = num_o / den_o
    num_h, den_h = woba_parts(sums_hand)
    return round(
        (num_h + LINEUP_SPLIT_SHRINK_PA * target) / (den_h + LINEUP_SPLIT_SHRINK_PA),
        4,
    )


def _weighted(slot_to_woba: dict[int, float | None], slots: range) -> float | None:
    """PA-share weighted wOBA over the given 1-indexed slots present.

    Renormalizes over the slots that are present AND have a non-None wOBA
    (D5): a missing 9-hole or a batter without a trailing-year line simply
    drops from both numerator and denominator. None when no slot is usable.
    """
    num = 0.0
    den = 0.0
    for slot in slots:
        value = slot_to_woba.get(slot)
        if value is None:
            continue
        share = LINEUP_PA_SHARE[slot - 1]
        num += share * value
        den += share
    return round(num / den, 4) if den > 0 else None


def weighted_lineup_woba(slot_to_woba: dict[int, float | None]) -> float | None:
    """PA-share weighted overall wOBA of the full 9-man order (D1/D5)."""
    return _weighted(slot_to_woba, range(1, 10))


def weighted_top4_woba(slot_to_vs_hand: dict[int, float | None]) -> float | None:
    """PA-share weighted vs-hand wOBA of the top 4 of the order (F5-critical)."""
    return _weighted(slot_to_vs_hand, range(1, TOP4_SLOTS + 1))
