"""POST /api/v1/analyze - on-demand analysis of a two-way MLB market.

The quantitative pipeline here is REAL (no-vig, edge, EV, Kelly, stake caps
from app.core). The model probability is a deterministic STUB: there is no
trained model yet. Every stubbed piece is marked with a TODO.
"""

import hashlib
from enum import Enum

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.core.devig import implied_prob, no_vig_two_way
from app.core.ev import edge as edge_fn
from app.core.ev import ev_per_unit
from app.core.kelly import kelly_fraction, stake

router = APIRouter(prefix="/api/v1", tags=["analyze"])

STUB_MODEL_VERSION = "stub-0.0.0"


class MarketType(str, Enum):
    MONEYLINE = "moneyline"
    F5_MONEYLINE = "f5_moneyline"


class AnalyzeRequest(BaseModel):
    """Two-way market to analyze, with the user's book prices.

    `reference_*_odds` are the no-vig reference line (Pinnacle in the MVP).
    If omitted, the user's book prices are de-vigged instead (less reliable:
    soft-book vig distribution is not a fair-probability estimate).
    """

    home_team: str = Field(min_length=1, examples=["Los Angeles Dodgers"])
    away_team: str = Field(min_length=1, examples=["San Diego Padres"])
    market: MarketType = MarketType.MONEYLINE
    home_decimal_odds: float = Field(gt=1.0, examples=[1.667])
    away_decimal_odds: float = Field(gt=1.0, examples=[2.30])
    reference_home_odds: float | None = Field(default=None, gt=1.0)
    reference_away_odds: float | None = Field(default=None, gt=1.0)
    bankroll: float = Field(default=1000.0, ge=0.0)
    kelly_user_fraction: float | None = Field(default=None, gt=0.0, le=1.0)
    stake_cap_pct: float | None = Field(default=None, gt=0.0, le=1.0)


class SideAnalysis(BaseModel):
    """Full quantitative breakdown for one side of the market."""

    selection: str
    decimal_odds: float
    implied_prob: float
    p_fair: float
    p_model: float
    edge: float
    ev_per_unit: float
    kelly_full: float
    stake_suggested: float
    publish: bool  # edge >= threshold AND ev >= threshold (calibration gate TODO)


class AnalyzeResponse(BaseModel):
    market: MarketType
    home_team: str
    away_team: str
    overround: float
    model_version: str
    calibration_gate_passed: bool | None  # None until a real model reports ECE
    sides: list[SideAnalysis]
    notes: list[str]


def _stub_model_probability(home_team: str, away_team: str, market: MarketType) -> float:
    """Deterministic placeholder for the home-win model probability.

    TODO(model): replace with the calibrated XGBoost/LightGBM model served
    from a versioned artifact (see docs/04-features-y-modelos.md). This stub
    hashes the matchup into [0.35, 0.65] so responses are stable across calls
    but carry NO predictive information. Never publish stub-based picks.
    """
    digest = hashlib.sha256(f"{market.value}:{home_team}@{away_team}".encode()).digest()
    unit = int.from_bytes(digest[:8], "big") / 2**64  # [0, 1)
    return 0.35 + 0.30 * unit


@router.post("/analyze", response_model=AnalyzeResponse)
def analyze(req: AnalyzeRequest, settings: Settings = Depends(get_settings)) -> AnalyzeResponse:
    """Analyze a two-way market: no-vig fair line, edge, EV and stake."""
    ref_home = req.reference_home_odds or req.home_decimal_odds
    ref_away = req.reference_away_odds or req.away_decimal_odds
    p_fair_home, p_fair_away, overround = no_vig_two_way(ref_home, ref_away)

    # TODO(model): real calibrated probabilities + as-of feature snapshot.
    p_model_home = _stub_model_probability(req.home_team, req.away_team, req.market)
    p_model_away = 1.0 - p_model_home

    user_fraction = req.kelly_user_fraction or settings.default_kelly_user_fraction
    cap_pct = req.stake_cap_pct or settings.default_stake_cap_pct

    sides: list[SideAnalysis] = []
    for selection, odds, p_fair, p_model in (
        (req.home_team, req.home_decimal_odds, p_fair_home, p_model_home),
        (req.away_team, req.away_decimal_odds, p_fair_away, p_model_away),
    ):
        side_edge = edge_fn(p_model, p_fair)
        side_ev = ev_per_unit(p_model, odds)
        kelly_full = kelly_fraction(p_model, odds)
        sides.append(
            SideAnalysis(
                selection=selection,
                decimal_odds=odds,
                implied_prob=implied_prob(odds),
                p_fair=p_fair,
                p_model=p_model,
                edge=side_edge,
                ev_per_unit=side_ev,
                kelly_full=kelly_full,
                stake_suggested=stake(req.bankroll, kelly_full, user_fraction, cap_pct),
                # TODO(calibration): also require ECE <= settings.ece_threshold
                # on a rolling 60-day window before publishing.
                publish=(
                    side_edge >= settings.edge_threshold and side_ev >= settings.ev_threshold
                ),
            )
        )

    return AnalyzeResponse(
        market=req.market,
        home_team=req.home_team,
        away_team=req.away_team,
        overround=overround,
        model_version=STUB_MODEL_VERSION,
        calibration_gate_passed=None,  # TODO(calibration): wire real ECE check
        sides=sides,
        notes=[
            "p_model is a deterministic stub with no predictive value; do not bet on it.",
            "Informational tool only: it does not place bets and does not promise profit.",
        ],
    )
