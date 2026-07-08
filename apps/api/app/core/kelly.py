"""Kelly criterion and final stake sizing.

Canonical definitions (see docs/05-motor-ev-y-bankroll.md):

- Full Kelly: ``f* = (p * (b + 1) - 1) / b`` with ``b = odds_decimal - 1``.
  Only positive when there is edge; the engine never returns a negative
  fraction (no edge -> 0.0, i.e. do not bet).
- Final stake: ``stake = bankroll * min(f* * user_fraction, cap_pct)``.
  Default user fraction is 1/8 (Kelly/8) and default cap is 2% of bankroll
  per pick. The cap always wins over the fractional Kelly suggestion.
"""


def kelly_fraction(p: float, decimal_odds: float) -> float:
    """Full Kelly fraction for a binary bet at ``decimal_odds``.

    ``f* = (p * (b + 1) - 1) / b`` with ``b = decimal_odds - 1``.

    Returns 0.0 when there is no positive edge (f* <= 0). Never negative:
    a negative Kelly means "bet the other side", which is a separate decision
    handled upstream, not a stake size.

    Raises:
        ValueError: if ``p`` is outside [0, 1] or ``decimal_odds <= 1.0``.
    """
    if not 0.0 <= p <= 1.0:
        raise ValueError(f"p must be in [0, 1], got {p}")
    if decimal_odds <= 1.0:
        raise ValueError(f"Decimal odds must be > 1.0, got {decimal_odds}")
    b = decimal_odds - 1.0
    f_star = (p * (b + 1.0) - 1.0) / b
    return max(0.0, f_star)


def stake(
    bankroll: float,
    kelly_full: float,
    user_fraction: float = 0.125,
    cap_pct: float = 0.02,
) -> float:
    """Final stake in bankroll currency units.

    ``stake = bankroll * min(kelly_full * user_fraction, cap_pct)``

    The engine computes full Kelly; the user chooses the fraction (default
    Kelly/8) and a hard cap per pick (default 2% of bankroll). The cap always
    dominates: even an aggressive Kelly suggestion never exceeds
    ``bankroll * cap_pct``.

    Raises:
        ValueError: if ``bankroll < 0``, ``kelly_full < 0``,
            ``user_fraction`` not in (0, 1], or ``cap_pct`` not in (0, 1].
    """
    if bankroll < 0:
        raise ValueError(f"bankroll must be >= 0, got {bankroll}")
    if kelly_full < 0:
        raise ValueError(f"kelly_full must be >= 0, got {kelly_full}")
    if not 0.0 < user_fraction <= 1.0:
        raise ValueError(f"user_fraction must be in (0, 1], got {user_fraction}")
    if not 0.0 < cap_pct <= 1.0:
        raise ValueError(f"cap_pct must be in (0, 1], got {cap_pct}")
    return bankroll * min(kelly_full * user_fraction, cap_pct)
