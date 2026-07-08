"""Edge and expected value per unit staked.

Canonical definitions (see docs/05-motor-ev-y-bankroll.md):

- Edge: ``edge = p_model - p_fair`` (in probability points).
- EV per unit staked: ``EV = p_model * (odds_decimal - 1) - (1 - p_model)``.

``p_model`` always comes from the calibrated statistical model, never from an
LLM. ``p_fair`` comes from the no-vig reference line (Pinnacle in the MVP).
"""

from app.core.devig import implied_prob  # noqa: F401  (re-exported convenience)


def _validate_prob(p: float, name: str) -> None:
    if not 0.0 <= p <= 1.0:
        raise ValueError(f"{name} must be in [0, 1], got {p}")


def edge(p_model: float, p_fair: float) -> float:
    """Edge in probability points: ``p_model - p_fair``.

    Positive edge means the model thinks the outcome is more likely than the
    no-vig market price implies.

    Raises:
        ValueError: if either probability is outside [0, 1].
    """
    _validate_prob(p_model, "p_model")
    _validate_prob(p_fair, "p_fair")
    return p_model - p_fair


def ev_per_unit(p_model: float, decimal_odds: float) -> float:
    """Expected value per 1 unit staked at ``decimal_odds``.

    ``EV = p_model * (decimal_odds - 1) - (1 - p_model)``

    e.g. ``p_model=0.55`` at decimal 2.00 -> EV = +0.10 units per unit staked.

    Raises:
        ValueError: if ``p_model`` is outside [0, 1] or ``decimal_odds <= 1.0``.
    """
    _validate_prob(p_model, "p_model")
    if decimal_odds <= 1.0:
        raise ValueError(f"Decimal odds must be > 1.0, got {decimal_odds}")
    return p_model * (decimal_odds - 1.0) - (1.0 - p_model)
