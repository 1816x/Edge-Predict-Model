"""Pure unit tests for app.ingestion.parsers (no network, no database)."""

from datetime import datetime, timezone

from app.ingestion.parsers import OddsEvent, parse_odds_event, parse_schedule

from conftest import load_fixture


class TestParseOddsEvent:
    def _event(self, index: int = 0) -> OddsEvent:
        return parse_odds_event(load_fixture("odds_api_mlb.json")[index])

    def test_identity_and_commence_time_utc(self):
        ev = self._event()
        assert ev.source_id == "e912aa27b1c4f03d8e5a6b7c8d9e0f1a"
        assert ev.home_team == "Boston Red Sox"
        assert ev.away_team == "New York Yankees"
        assert ev.commence_time == datetime(2026, 7, 8, 23, 10, tzinfo=timezone.utc)

    def test_market_mapping_and_counts(self):
        ev = self._event()
        # pinnacle h2h (2) + fanduel h2h (2); fanduel spreads is not an MVP
        # market. The F5 market never appears slate-wide (additional market).
        assert len(ev.outcomes) == 4
        assert {o.market for o in ev.outcomes} == {"moneyline"}
        assert "fanduel:market:spreads" in ev.skipped

    def test_per_event_f5_payload_parses_with_draw_skipped(self):
        # Same parser handles the per-event endpoint's single-event shape.
        ev = parse_odds_event(load_fixture("odds_api_event_f5.json"))
        assert ev.source_id == "e912aa27b1c4f03d8e5a6b7c8d9e0f1a"
        assert len(ev.outcomes) == 2
        assert {o.market for o in ev.outcomes} == {"f5_moneyline"}
        assert "pinnacle:f5_moneyline:outcome:Draw" in ev.skipped
        f5 = ev.outcomes[0]
        assert f5.last_update == datetime(2026, 7, 8, 17, 58, 41, tzinfo=timezone.utc)

    def test_sides_resolved_from_team_names(self):
        ev = self._event()
        pinnacle_ml = {
            o.side: o for o in ev.outcomes if o.book_key == "pinnacle" and o.market == "moneyline"
        }
        assert pinnacle_ml["home"].price_decimal == 2.05
        assert pinnacle_ml["away"].price_decimal == 1.86

    def test_american_prices_derived_from_decimal(self):
        ev = self._event()
        by_key = {(o.book_key, o.market, o.side): o for o in ev.outcomes}
        assert by_key[("pinnacle", "moneyline", "home")].price_american == 105
        assert by_key[("pinnacle", "moneyline", "away")].price_american == -116
        assert by_key[("fanduel", "moneyline", "home")].price_american == 110

    def test_junk_price_is_skipped(self):
        payload = load_fixture("odds_api_mlb.json")[1]
        payload["bookmakers"][0]["markets"][0]["outcomes"][0]["price"] = 1.0005
        ev = parse_odds_event(payload)
        assert len(ev.outcomes) == 1  # only the valid away price remains
        assert any(":price:" in s for s in ev.skipped)


class TestParseSchedule:
    def test_games_and_probables(self):
        games = parse_schedule(load_fixture("mlb_schedule.json"))
        assert [g.game_pk for g in games] == [745001, 745002, 745003]
        g1 = games[0]
        assert g1.home_name == "Boston Red Sox"
        assert g1.away_name == "New York Yankees"
        assert g1.home_mlb_id == 111 and g1.away_mlb_id == 147
        assert g1.home_probable == "Nick Pivetta"
        assert g1.away_probable == "Gerrit Cole"
        assert g1.start_time == datetime(2026, 7, 8, 23, 10, tzinfo=timezone.utc)
        # Doubleheader nightcap has no probables announced yet.
        assert games[2].home_probable is None

    def test_status_mapping(self):
        def one_game(abstract: str, detailed: str) -> str:
            payload = {
                "dates": [
                    {
                        "games": [
                            {
                                "gamePk": 1,
                                "gameDate": "2026-07-08T20:00:00Z",
                                "status": {
                                    "abstractGameState": abstract,
                                    "detailedState": detailed,
                                },
                                "teams": {
                                    "home": {"team": {"id": 1, "name": "H"}},
                                    "away": {"team": {"id": 2, "name": "A"}},
                                },
                            }
                        ]
                    }
                ]
            }
            return parse_schedule(payload)[0].status

        assert one_game("Preview", "Scheduled") == "scheduled"
        assert one_game("Live", "In Progress") == "live"
        assert one_game("Final", "Final") == "final"
        assert one_game("Final", "Postponed") == "postponed"
        assert one_game("Final", "Cancelled") == "cancelled"
        assert one_game("SomethingNew", "Warmup") == "scheduled"
