"""Odds conversion and de-vigging (multiplicative / proportional method).

Canonical definitions (see docs/05-motor-ev-y-bankroll.md):

- Implied probability: ``p_imp = 1 / odds_decimal``.
- No-vig, two-way market, multiplicative method (MVP default):
  ``p_fair_i = p_imp_i / (p_imp_1 + p_imp_2)``.

The multiplicative method is the MVP default. Better methods exist for the
favorite-longshot bias (Shin, power method); they are a future improvement.
"""


def american_to_decimal(american: float) -> float:
    """Convert American odds to decimal (European) odds.

    ``-150 -> 1.6667`` (risk 150 to win 100), ``+130 -> 2.30`` (risk 100 to
    win 130). Valid American odds satisfy ``abs(american) >= 100``.

    Raises:
        ValueError: if ``abs(american) < 100``.
    """
    if abs(american) < 100:
        raise ValueError(f"American odds must satisfy abs(odds) >= 100, got {american}")
    if american > 0:
        return 1.0 + american / 100.0
    return 1.0 + 100.0 / abs(american)


def decimal_to_american(decimal_odds: float) -> float:
    """Convert decimal odds to American odds.

    ``2.30 -> +130``, ``1.6667 -> -150``. Decimal odds of exactly 2.0 map to
    +100.

    Raises:
        ValueError: if ``decimal_odds <= 1.0`` (no valid price pays less than
            the stake back).
    """
    if decimal_odds <= 1.0:
        raise ValueError(f"Decimal odds must be > 1.0, got {decimal_odds}")
    if decimal_odds >= 2.0:
        return (decimal_odds - 1.0) * 100.0
    return -100.0 / (decimal_odds - 1.0)


def implied_prob(decimal_odds: float) -> float:
    """Implied (vigged) probability of decimal odds: ``p_imp = 1 / odds``.

    Raises:
        ValueError: if ``decimal_odds <= 1.0``.
    """
    if decimal_odds <= 1.0:
        raise ValueError(f"Decimal odds must be > 1.0, got {decimal_odds}")
    return 1.0 / decimal_odds


def no_vig_two_way(decimal_odds_a: float, decimal_odds_b: float) -> tuple[float, float, float]:
    """Remove the vig from a two-way market with the multiplicative method.

    Args:
        decimal_odds_a: decimal odds for side A (e.g. home / favorite).
        decimal_odds_b: decimal odds for side B (e.g. away / underdog).

    Returns:
        ``(p_fair_a, p_fair_b, overround)`` where
        ``p_fair_i = p_imp_i / (p_imp_a + p_imp_b)`` and
        ``overround = p_imp_a + p_imp_b`` (e.g. 1.0348 = 3.48% vig).
        ``p_fair_a + p_fair_b == 1.0`` up to floating point.

    Raises:
        ValueError: if either odds value is <= 1.0, or if the implied
            probabilities sum to less than 1.0 (that is not a vigged two-way
            market: it is an arbitrage or inconsistent input, and the
            multiplicative method would inflate both probabilities).
    """
    p_a = implied_prob(decimal_odds_a)
    p_b = implied_prob(decimal_odds_b)
    overround = p_a + p_b
    if overround < 1.0:
        raise ValueError(
            f"Implied probabilities sum to {overround:.4f} < 1.0; "
            "not a valid vigged two-way market (arbitrage or bad input)"
        )
    return p_a / overround, p_b / overround, overround
