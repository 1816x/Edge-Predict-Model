"""Tests for Kelly fraction and capped stake sizing."""

import pytest

from app.core.kelly import kelly_fraction, stake


class TestKellyFraction:
    def test_positive_edge(self):
        # p=0.62 at decimal 2.0: b=1, f* = (0.62*2 - 1)/1 = 0.24.
        assert kelly_fraction(0.62, 2.0) == pytest.approx(0.24)

    def test_underdog_case(self):
        # p=0.45 at decimal 2.30: b=1.3, f* = (0.45*2.3 - 1)/1.3 = 0.0269231.
        assert kelly_fraction(0.45, 2.30) == pytest.approx(0.0269231, abs=1e-6)

    def test_no_edge_returns_zero(self):
        # p exactly at implied probability -> f* = 0.
        assert kelly_fraction(0.5, 2.0) == 0.0

    def test_negative_edge_clamped_to_zero_never_negative(self):
        # p below implied probability -> raw f* < 0, must clamp to 0.0.
        assert kelly_fraction(0.40, 2.0) == 0.0
        assert kelly_fraction(0.10, 1.50) == 0.0
        assert kelly_fraction(0.0, 5.0) == 0.0

    def test_certain_win_bets_everything(self):
        assert kelly_fraction(1.0, 2.0) == pytest.approx(1.0)

    @pytest.mark.parametrize("p", [-0.01, 1.01])
    def test_invalid_prob_raises(self, p):
        with pytest.raises(ValueError):
            kelly_fraction(p, 2.0)

    @pytest.mark.parametrize("odds", [1.0, 0.5, -2.0])
    def test_invalid_odds_raise(self, odds):
        with pytest.raises(ValueError):
            kelly_fraction(0.5, odds)


class TestStake:
    def test_cap_dominates(self):
        # Full Kelly 0.24, Kelly/8 = 0.03 > cap 0.02 -> stake = 1000 * 0.02 = 20.
        assert stake(1000.0, 0.24, user_fraction=0.125, cap_pct=0.02) == pytest.approx(20.0)

    def test_fractional_kelly_below_cap(self):
        # Full Kelly 0.08, Kelly/8 = 0.01 < cap 0.02 -> stake = 1000 * 0.01 = 10.
        assert stake(1000.0, 0.08, user_fraction=0.125, cap_pct=0.02) == pytest.approx(10.0)

    def test_defaults_are_kelly_eighth_and_two_pct_cap(self):
        # Same numbers via defaults.
        assert stake(1000.0, 0.24) == pytest.approx(20.0)
        assert stake(1000.0, 0.08) == pytest.approx(10.0)

    def test_zero_kelly_means_zero_stake(self):
        assert stake(1000.0, 0.0) == 0.0

    def test_zero_bankroll_means_zero_stake(self):
        assert stake(0.0, 0.24) == 0.0

    def test_negative_bankroll_raises(self):
        with pytest.raises(ValueError):
            stake(-100.0, 0.24)

    def test_negative_kelly_raises(self):
        # kelly_fraction never returns negatives; passing one in is a bug.
        with pytest.raises(ValueError):
            stake(1000.0, -0.1)

    @pytest.mark.parametrize("fraction", [0.0, -0.5, 1.5])
    def test_invalid_user_fraction_raises(self, fraction):
        with pytest.raises(ValueError):
            stake(1000.0, 0.24, user_fraction=fraction)

    @pytest.mark.parametrize("cap", [0.0, -0.02, 1.5])
    def test_invalid_cap_raises(self, cap):
        with pytest.raises(ValueError):
            stake(1000.0, 0.24, cap_pct=cap)
