"""Shared offense-block math (docs/04 §1.2) for builder and dataset.

The online builder (app/features/builder.py) and the bulk dataset builder
(app/ml/dataset.py) both compute the team-offense features; formula skew
between the two is exactly the train/serve-skew bug class the parity test
guards against, so the FORMULAS live here once, operating on plain
aggregate sums. The WINDOW semantics (UTC-day boundaries, opposing-hand
classification) still live in each path and remain guarded by
tests/test_ml.py::test_bulk_features_match_online_builder.

wOBA linear weights are FanGraphs' published 2017 constants — the season
immediately BEFORE the earliest archived season (2018), so they are as-of
valid for every row of the dataset (docs/04 §4 checklist item 9: never
current-season-final constants). Weights drift with the run environment,
but a model feature only cares about ordering and stability, and freezing
them keeps every row on one scale — the same argument that drops xFIP's
additive constant in the starter block. Recorded in docs/00-decisiones.md.

wOBA (FanGraphs basic form, no SB/CS terms):
    num = wBB*uBB + wHBP*HBP + w1B*1B + w2B*2B + w3B*3B + wHR*HR
    den = AB + BB - IBB + SF + HBP        (uBB = BB - IBB, 1B derived)
K%/BB% use the DERIVED plate-appearance denominator AB+BB+HBP+SF+SH,
never the feed's plateAppearances field (cross-era uniformity; catcher's
interference excluded uniformly).
"""

from __future__ import annotations

OFFENSE_ROLLING_DAYS = 30
# Split shrink target: the team's own trailing-year same-hand split. One
# continuous mechanism implements docs/04 §1.2's "shrink to the season
# split, and in April to weighted previous season" (in April the trailing
# year IS mostly the previous season), mirroring the starter block's
# 365-day windows.
OFFENSE_SPLIT_TARGET_DAYS = 365
# Pseudo-PA pulling the 30d split toward the trailing-year split (~5 team
# games of wOBA denominators). Boring and stable beats reactive (§1.1).
TEAM_SPLIT_SHRINK_PA = 200.0

# FanGraphs 2017 wOBA linear weights (pre-dataset, hence leak-free).
WOBA_W_UBB = 0.693
WOBA_W_HBP = 0.723
WOBA_W_1B = 0.877
WOBA_W_2B = 1.232
WOBA_W_3B = 1.552
WOBA_W_HR = 1.980

OFFENSE_FEATURE_NAMES = (
    "team_woba_30d",
    "team_woba_season",
    "team_woba_vs_opp_hand_30d",
    "team_iso_30d",
    "team_k_pct_30d",
    "team_bb_pct_30d",
    "team_ops_30d",
)

# Aggregate-sum keys every window must provide; they are exactly the
# batting_game_logs counting-column names so both paths sum the same
# columns by construction.
SUM_KEYS = (
    "at_bats",
    "hits",
    "doubles",
    "triples",
    "home_runs",
    "walks",
    "intentional_walks",
    "strikeouts",
    "hit_by_pitch",
    "sac_flies",
    "sac_bunts",
)


def woba_parts(s: dict) -> tuple[float, float]:
    """(numerator, denominator) of wOBA over aggregate sums."""
    singles = s["hits"] - s["doubles"] - s["triples"] - s["home_runs"]
    ubb = s["walks"] - s["intentional_walks"]
    num = (
        WOBA_W_UBB * ubb
        + WOBA_W_HBP * s["hit_by_pitch"]
        + WOBA_W_1B * singles
        + WOBA_W_2B * s["doubles"]
        + WOBA_W_3B * s["triples"]
        + WOBA_W_HR * s["home_runs"]
    )
    den = (
        s["at_bats"] + s["walks"] - s["intentional_walks"]
        + s["sac_flies"] + s["hit_by_pitch"]
    )
    return num, den


def woba(s: dict) -> float | None:
    """wOBA over aggregate sums, or None without a denominator."""
    num, den = woba_parts(s)
    return round(num / den, 4) if den > 0 else None


def offense_rates(s: dict) -> dict[str, float | None]:
    """The non-split window rates from aggregate sums.

    Empty windows yield None per stat: a rate without plate appearances
    is not evidence of anything — zeros are never fabricated. OPS adds
    UNROUNDED OBP and SLG, then rounds once (both paths must agree to the
    digit).
    """
    pa = s["at_bats"] + s["walks"] + s["hit_by_pitch"] + s["sac_flies"] + s["sac_bunts"]
    total_bases = s["hits"] + s["doubles"] + 2 * s["triples"] + 3 * s["home_runs"]
    obp_den = s["at_bats"] + s["walks"] + s["hit_by_pitch"] + s["sac_flies"]
    out: dict[str, float | None] = {
        "team_woba_30d": woba(s),
        "team_iso_30d": None,
        "team_k_pct_30d": None,
        "team_bb_pct_30d": None,
        "team_ops_30d": None,
    }
    if s["at_bats"] > 0:
        out["team_iso_30d"] = round((total_bases - s["hits"]) / s["at_bats"], 4)
    if pa > 0:
        out["team_k_pct_30d"] = round(s["strikeouts"] / pa, 4)
        out["team_bb_pct_30d"] = round(s["walks"] / pa, 4)
    if obp_den > 0 and s["at_bats"] > 0:
        obp = (s["hits"] + s["walks"] + s["hit_by_pitch"]) / obp_den
        slg = total_bases / s["at_bats"]
        out["team_ops_30d"] = round(obp + slg, 4)
    return out


def shrunk_split(
    num_30: float, den_30: float, num_365: float, den_365: float
) -> float | None:
    """30d same-hand wOBA shrunk toward the trailing-year same-hand split.

    (num30 + C*target) / (den30 + C) with C = TEAM_SPLIT_SHRINK_PA. With
    no trailing-year sample there is no target and the answer is None —
    never fabricated. With an empty 30d window the value IS the target
    (pure prior: e.g. a month without facing lefty starters).
    """
    if den_365 <= 0:
        return None
    target = num_365 / den_365
    return round(
        (num_30 + TEAM_SPLIT_SHRINK_PA * target) / (den_30 + TEAM_SPLIT_SHRINK_PA), 4
    )
