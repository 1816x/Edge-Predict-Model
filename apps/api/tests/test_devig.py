"""Tests for odds conversion and multiplicative de-vigging.

Hand-worked reference case: American -150 / +130.
  decimals:      1 + 100/150 = 1.66667 | 1 + 130/100 = 2.30
  implied:       0.60000 | 0.4347826...
  overround:     1.0347826...
  fair (mult.):  0.6/1.0347826 = 0.5798319... | 0.4347826/1.0347826 = 0.4201681...
"""

import pytest

from app.core.devig import (
    american_to_decimal,
    decimal_to_american,
    implied_prob,
    no_vig_two_way,
)


class TestAmericanToDecimal:
    def test_favorite(self):
        assert american_to_decimal(-150) == pytest.approx(1.6666667)

    def test_underdog(self):
        assert american_to_decimal(130) == pytest.approx(2.30)

    def test_even_money(self):
        assert american_to_decimal(100) == pytest.approx(2.0)
        assert american_to_decimal(-100) == pytest.approx(2.0)

    @pytest.mark.parametrize("bad", [0, 50, -50, 99, -99])
    def test_invalid_american_raises(self, bad):
        with pytest.raises(ValueError):
            american_to_decimal(bad)


class TestDecimalToAmerican:
    def test_underdog(self):
        assert decimal_to_american(2.30) == pytest.approx(130.0)

    def test_favorite(self):
        assert decimal_to_american(1.6666667) == pytest.approx(-150.0)

    def test_even_money(self):
        assert decimal_to_american(2.0) == pytest.approx(100.0)

    def test_roundtrip(self):
        for american in (-250, -150, -110, 100, 130, 240):
            assert decimal_to_american(american_to_decimal(american)) == pytest.approx(american)

    @pytest.mark.parametrize("bad", [1.0, 0.5, 0.0, -2.0])
    def test_invalid_decimal_raises(self, bad):
        with pytest.raises(ValueError):
            decimal_to_american(bad)


class TestImpliedProb:
    def test_favorite(self):
        assert implied_prob(1.6666667) == pytest.approx(0.60, abs=1e-6)

    def test_underdog(self):
        assert implied_prob(2.30) == pytest.approx(0.4347826, abs=1e-6)

    @pytest.mark.parametrize("bad", [1.0, 0.99, 0.0, -1.5])
    def test_invalid_odds_raises(self, bad):
        with pytest.raises(ValueError):
            implied_prob(bad)


class TestNoVigTwoWay:
    def test_reference_case_minus150_plus130(self):
        p_fair_fav, p_fair_dog, overround = no_vig_two_way(1.6666667, 2.30)
        assert overround == pytest.approx(1.0347826, abs=1e-6)
        assert p_fair_fav == pytest.approx(0.5798319, abs=1e-6)
        assert p_fair_dog == pytest.approx(0.4201681, abs=1e-6)

    def test_fair_probs_sum_to_one(self):
        p_a, p_b, _ = no_vig_two_way(1.91, 1.91)
        assert p_a + p_b == pytest.approx(1.0)
        assert p_a == pytest.approx(0.5)

    def test_vig_free_market_passes_through(self):
        # Exactly fair two-way market: overround == 1.0, probs unchanged.
        p_a, p_b, overround = no_vig_two_way(2.0, 2.0)
        assert overround == pytest.approx(1.0)
        assert p_a == pytest.approx(0.5)
        assert p_b == pytest.approx(0.5)

    def test_implied_probs_summing_below_one_raise(self):
        # 2.10 / 2.10 -> 0.4762 + 0.4762 = 0.9524 < 1: arbitrage, not a
        # vigged market; the multiplicative method must refuse it.
        with pytest.raises(ValueError):
            no_vig_two_way(2.10, 2.10)

    @pytest.mark.parametrize("odds_a,odds_b", [(1.0, 2.0), (2.0, 1.0), (0.5, 2.0)])
    def test_invalid_odds_raise(self, odds_a, odds_b):
        with pytest.raises(ValueError):
            no_vig_two_way(odds_a, odds_b)
