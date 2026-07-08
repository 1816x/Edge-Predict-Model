"""GET /api/v1/performance - typed stub of the performance report.

Winrate is deliberately NOT the headline metric. The report separates
calibration (Brier, log loss, ECE), profitability (yield vs ROI, distinct
definitions) and process quality (CLV). See docs/06-backtesting-y-metricas.md.

TODO(metrics): compute everything from graded picks in Postgres once
persistence and grading exist; all values below are placeholders.
"""

from datetime import date

from fastapi import APIRouter
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/v1", tags=["performance"])


class PerformanceOut(BaseModel):
    """Aggregated performance over a period. Null = not yet computable."""

    period_start: date | None = None
    period_end: date | None = None
    n_picks: int = 0
    units_staked: float = 0.0
    net_profit_units: float = 0.0
    # Yield = net profit / total staked. ROI = net profit / starting bankroll
    # of the period. They are different metrics; never conflate them.
    yield_pct: float | None = None
    roi_pct: float | None = None
    # Calibration of the model's probabilities (the metric that matters most).
    brier_score: float | None = None
    log_loss: float | None = None
    ece: float | None = Field(default=None, description="Expected calibration error")
    # Process quality vs the no-vig Pinnacle close.
    avg_clv_prob_pts: float | None = None
    clv_beat_rate: float | None = None
    max_drawdown_units: float | None = None
    hit_rate: float | None = Field(
        default=None,
        description="Informational only; hit rate alone says nothing about profitability",
    )


@router.get("/performance", response_model=PerformanceOut)
def performance() -> PerformanceOut:
    """Performance report stub.

    TODO(metrics): aggregate graded picks (walk-forward backtest first, then
    paper trading, then live) and fill every field with real values.
    """
    return PerformanceOut()
