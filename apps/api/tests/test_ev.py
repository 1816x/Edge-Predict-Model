"""Tests for edge and EV per unit staked."""

import pytest

from app.core.ev import edge, ev_per_unit


class TestEdge:
    def test_positive_edge(self):
        # p_model 0.60 vs fair 0.5798319 (from the -150/+130 no-vig case).
        assert edge(0.60, 0.5798319) == pytest.approx(0.0201681, abs=1e-6)

    def test_negative_edge(self):
        assert edge(0.55, 0.5798319) == pytest.approx(-0.0298319, abs=1e-6)

    def test_zero_edge(self):
        assert edge(0.5, 0.5) == pytest.approx(0.0)

    @pytest.mark.parametrize("p_model,p_fair", [(-0.1, 0.5), (1.1, 0.5), (0.5, -0.1), (0.5, 1.1)])
    def test_out_of_range_probs_raise(self, p_model, p_fair):
        with pytest.raises(ValueError):
            edge(p_model, p_fair)


class TestEvPerUnit:
    def test_positive_ev(self):
        # 0.55 * (2.0 - 1) - 0.45 = +0.10 units per unit staked.
        assert ev_per_unit(0.55, 2.0) == pytest.approx(0.10)

    def test_negative_ev(self):
        # 0.40 * (2.0 - 1) - 0.60 = -0.20.
        assert ev_per_unit(0.40, 2.0) == pytest.approx(-0.20)

    def test_breakeven_at_implied_prob(self):
        # p_model equal to the implied probability -> EV exactly 0.
        assert ev_per_unit(0.5, 2.0) == pytest.approx(0.0)
        assert ev_per_unit(1 / 2.30, 2.30) == pytest.approx(0.0)

    def test_certain_win(self):
        assert ev_per_unit(1.0, 2.30) == pytest.approx(1.30)

    def test_certain_loss(self):
        assert ev_per_unit(0.0, 2.30) == pytest.approx(-1.0)

    @pytest.mark.parametrize("p", [-0.01, 1.01])
    def test_invalid_prob_raises(self, p):
        with pytest.raises(ValueError):
            ev_per_unit(p, 2.0)

    @pytest.mark.parametrize("odds", [1.0, 0.9, 0.0, -1.0])
    def test_invalid_odds_raise(self, odds):
        with pytest.raises(ValueError):
            ev_per_unit(0.5, odds)
