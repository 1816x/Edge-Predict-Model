"""Pure unit tests for app.features.transactions (no network, no database).

Hand-computed classifier cases, including the pre-2019 "disabled list" wording
that the 2018 backfill must still classify (the DL was renamed the IL in 2019).
"""

from datetime import date

from app.features.transactions import (
    il_effect,
    il_out_asof,
    mentions_il,
    top_k_star_players,
)


class TestIlEffect:
    def test_placement_on_injured_list_is_out(self):
        assert (
            il_effect(
                "SC", "Status Change",
                "New York Yankees placed RF Aaron Judge on the 10-day injured list.",
            )
            == 1
        )

    def test_activation_from_injured_list_is_back(self):
        assert (
            il_effect(
                "SC", "Status Change",
                "Los Angeles Angels activated CF Mike Trout from the 10-day injured list.",
            )
            == -1
        )

    def test_transfer_between_il_tiers_keeps_player_out(self):
        # 10-day -> 60-day IL: the player is STILL out, so it is a +1.
        assert (
            il_effect(
                "SC", "Status Change",
                "Chicago Cubs transferred RHP X to the 60-day injured list.",
            )
            == 1
        )

    def test_pre_2019_disabled_list_placement_still_classifies(self):
        # 2018 backfill: MLB called it the DISABLED list before 2019. This is
        # the exact case a naive "injured list"-only matcher would silently
        # miss across a whole season of history.
        assert (
            il_effect(
                "SC", "Status Change",
                "Boston Red Sox placed LHP Y on the 10-day disabled list.",
            )
            == 1
        )

    def test_pre_2019_disabled_list_activation_still_classifies(self):
        assert (
            il_effect(
                "SC", "Status Change",
                "Boston Red Sox activated LHP Y from the 10-day disabled list.",
            )
            == -1
        )

    def test_trade_is_not_an_il_move(self):
        assert (
            il_effect(
                "TR", "Trade",
                "Los Angeles Dodgers traded RF Mookie Betts to the Boston Red Sox.",
            )
            is None
        )

    def test_recall_is_not_an_il_move(self):
        assert il_effect("SC", "Status Change", "Team recalled RHP Z from Triple-A.") is None

    def test_il_mention_without_a_verb_is_unclassified_not_guessed(self):
        # Names the IL but no recognized verb: return None (do not guess), and
        # the caller's drift canary (mentions_il) flags it.
        desc = "Roster note referencing the injured list with no move verb."
        assert il_effect("SC", "Status Change", desc) is None
        assert mentions_il("Status Change", desc) is True

    def test_mentions_il_covers_both_names(self):
        assert mentions_il(None, "placed on the injured list") is True
        assert mentions_il(None, "placed on the disabled list") is True
        assert mentions_il("Trade", "traded to the Red Sox") is False


class TestIlOutAsof:
    G = date(2024, 4, 20)  # the game day (as-of cut is < G)

    def test_no_moves_is_available(self):
        assert il_out_asof([], self.G) is False

    def test_latest_move_before_cut_is_a_placement(self):
        moves = [(date(2024, 4, 10), 1, 1)]
        assert il_out_asof(moves, self.G) is True

    def test_latest_move_is_an_activation(self):
        moves = [(date(2024, 4, 10), 1, 1), (date(2024, 4, 18), 2, -1)]
        assert il_out_asof(moves, self.G) is False

    def test_move_on_the_game_day_is_unknown(self):
        # date == event_day is NOT < event_day: a same-day move is not yet known.
        moves = [(date(2024, 4, 20), 5, 1)]
        assert il_out_asof(moves, self.G) is False

    def test_same_date_reversal_breaks_by_transaction_id(self):
        # Placed (id 10) then activated (id 11) on the SAME earlier date: the
        # higher id is the later move -> activated -> available. The tiebreak is
        # identical in the online and bulk paths, so both agree.
        placed_then_activated = [(date(2024, 4, 12), 10, 1), (date(2024, 4, 12), 11, -1)]
        assert il_out_asof(placed_then_activated, self.G) is False
        # Reverse ids: activated (10) then placed (11) same day -> still out.
        activated_then_placed = [(date(2024, 4, 12), 11, 1), (date(2024, 4, 12), 10, -1)]
        assert il_out_asof(activated_then_placed, self.G) is True


class TestTopKStarPlayers:
    def _sums(self, ab, bb=0, hits=0, hr=0):
        return {
            "at_bats": ab, "hits": hits, "doubles": 0, "triples": 0, "home_runs": hr,
            "walks": bb, "intentional_walks": 0, "strikeouts": 0, "hit_by_pitch": 0,
            "sac_flies": 0, "sac_bunts": 0,
        }

    def test_thin_samples_are_not_stars(self):
        # den = at_bats + walks; below LINEUP_STAR_MIN_PA (200) -> not a star.
        players = {"a": self._sums(ab=100, hits=40, hr=10)}
        assert top_k_star_players(players) == []

    def test_ranks_by_woba_and_caps_at_k(self):
        players = {
            "weak": self._sums(ab=300, hits=60, hr=2),    # low wOBA, qualifies
            "strong": self._sums(ab=300, hits=120, hr=30),  # high wOBA
            "mid": self._sums(ab=300, hits=90, hr=15),
        }
        assert top_k_star_players(players, k=2) == ["strong", "mid"]

    def test_tiebreak_is_deterministic_by_player_id_string(self):
        # Two identical lines -> identical wOBA -> ordered by str(player_id).
        line = self._sums(ab=300, hits=90, hr=15)
        players = {"zeta": dict(line), "alpha": dict(line)}
        assert top_k_star_players(players, k=1) == ["alpha"]
