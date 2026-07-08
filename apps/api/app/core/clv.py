"""Closing Line Value (CLV) against the no-vig closing line.

Convention used across the project (see docs/05-motor-ev-y-bankroll.md and
docs/06-backtesting-y-metricas.md):

- The *taken* price is the decimal odds the user actually bet at (vig
  included, because that is the real price paid).
- The *closing* reference is the Pinnacle closing line with the vig removed
  (multiplicative method, see :mod:`app.core.devig`).
- CLV in probability points:
  ``clv = p_fair_close - implied_prob(taken_decimal)``.
  Positive CLV means the market closed with a higher fair probability than
  the price you paid implied, i.e. you got a better (longer) price than the
  fair close: you beat the close.
- Beat rate: fraction of picks with ``taken_decimal > closing_fair_decimal``,
  computed per pick by :func:`beat_close`.

CLV is a process metric: it does not guarantee profit on any single bet, but
sustained positive CLV against a sharp book is strong evidence the picks have
real edge.
"""

from app.core.devig import implied_prob


def clv_prob_pts(taken_decimal: float, closing_fair_prob: float) -> float:
    """CLV in probability points: ``closing_fair_prob - 1 / taken_decimal``.

    Args:
        taken_decimal: decimal odds actually taken (vig included).
        closing_fair_prob: no-vig probability of the same selection at close
            (from :func:`app.core.devig.no_vig_two_way` on the closing lines).

    Returns:
        Positive when the bet beat the close (paid price implied a lower
        probability than the fair close), negative when it closed worse.

    Raises:
        ValueError: if ``taken_decimal <= 1.0`` or ``closing_fair_prob`` is
            outside [0, 1].
    """
    if not 0.0 <= closing_fair_prob <= 1.0:
        raise ValueError(f"closing_fair_prob must be in [0, 1], got {closing_fair_prob}")
    return closing_fair_prob - implied_prob(taken_decimal)


def beat_close(taken_decimal: float, closing_decimal_no_vig: float) -> bool:
    """True if the taken price beat the no-vig closing price.

    Convention: you beat the close when your decimal odds are strictly
    greater than the fair (no-vig) closing decimal odds for the same
    selection, i.e. you were paid more than the closing consensus considered
    fair. Equal prices do not count as beating the close.

    Raises:
        ValueError: if either odds value is <= 1.0.
    """
    if taken_decimal <= 1.0:
        raise ValueError(f"taken_decimal must be > 1.0, got {taken_decimal}")
    if closing_decimal_no_vig <= 1.0:
        raise ValueError(f"closing_decimal_no_vig must be > 1.0, got {closing_decimal_no_vig}")
    return taken_decimal > closing_decimal_no_vig
