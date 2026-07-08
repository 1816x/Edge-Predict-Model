"""Tests for CLV in probability points and beat-the-close.

Reference closing market: -150/+130 -> no-vig underdog prob 0.4201681
(see test_devig.py), fair underdog decimal = 1/0.4201681 = 2.38.
"""

import pytest

from app.core.clv import beat_close, clv_prob_pts
from app.core.devig import no_vig_two_way

CLOSING_FAIR_DOG_PROB = 0.4201681  # from -150/+130 multiplicative no-vig
CLOSING_FAIR_DOG_DECIMAL = 1 / CLOSING_FAIR_DOG_PROB  # 2.38


class TestClvProbPts:
    def test_positive_clv_when_taken_price_longer_than_fair_close(self):
        # Took 2.50 on the dog; implied 0.40 < fair close 0.4201681.
        # CLV = 0.4201681 - 0.40 = +0.0201681 probability points.
        assert clv_prob_pts(2.50, CLOSING_FAIR_DOG_PROB) == pytest.approx(0.0201681, abs=1e-6)

    def test_negative_clv_when_line_moved_against(self):
        # Took 2.10; implied 0.4761905 > fair close 0.4201681.
        # CLV = 0.4201681 - 0.4761905 = -0.0560224.
        assert clv_prob_pts(2.10, CLOSING_FAIR_DOG_PROB) == pytest.approx(-0.0560224, abs=1e-6)

    def test_zero_clv_at_fair_closing_price(self):
        assert clv_prob_pts(CLOSING_FAIR_DOG_DECIMAL, CLOSING_FAIR_DOG_PROB) == pytest.approx(
            0.0, abs=1e-9
        )

    def test_consistent_with_devig_pipeline(self):
        # End to end: de-vig the closing market, then score a taken price.
        _, p_fair_dog, _ = no_vig_two_way(1.6666667, 2.30)
        assert clv_prob_pts(2.50, p_fair_dog) == pytest.approx(0.0201681, abs=1e-6)

    def test_invalid_taken_odds_raise(self):
        with pytest.raises(ValueError):
            clv_prob_pts(1.0, 0.5)

    @pytest.mark.parametrize("bad_prob", [-0.01, 1.01])
    def test_invalid_closing_prob_raises(self, bad_prob):
        with pytest.raises(ValueError):
            clv_prob_pts(2.0, bad_prob)


class TestBeatClose:
    def test_beat(self):
        assert beat_close(2.50, CLOSING_FAIR_DOG_DECIMAL) is True

    def test_not_beat(self):
        assert beat_close(2.10, CLOSING_FAIR_DOG_DECIMAL) is False

    def test_equal_price_does_not_beat(self):
        assert beat_close(2.38, 2.38) is False

    @pytest.mark.parametrize("taken,closing", [(1.0, 2.0), (2.0, 1.0), (0.5, 0.5)])
    def test_invalid_odds_raise(self, taken, closing):
        with pytest.raises(ValueError):
            beat_close(taken, closing)
