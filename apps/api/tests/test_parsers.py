"""Pure unit tests for app.ingestion.parsers (no network, no database)."""

from datetime import datetime, timezone

from app.ingestion.parsers import (
    OddsEvent,
    parse_boxscore_pitching,
    parse_odds_event,
    parse_schedule,
)

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
        # Person ids are the durable identity event_probables stores.
        assert g1.home_probable_id == 601713
        assert g1.away_probable_id == 543037
        assert games[2].home_probable_id is None
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


class TestParseBoxscorePitching:
    def _parsed(self):
        return parse_boxscore_pitching(load_fixture("mlb_boxscore.json"))

    def test_all_pitching_lines_kept_and_position_players_ignored(self):
        box = self._parsed()
        # Home starter + home reliever + away starter; the away reliever is
        # dropped (missing battersFaced) and the catcher never pitched.
        assert {l.mlb_person_id for l in box.lines} == {608566, 700001, 700002}

    def test_starter_flag_from_games_started(self):
        box = self._parsed()
        by_id = {l.mlb_person_id: l for l in box.lines}
        assert by_id[608566].is_starter is True
        assert by_id[700001].is_starter is False

    def test_starter_fallback_to_appearance_order(self):
        # No away line carries gamesStarted: the first id in the team's
        # `pitchers` list is the starter, and the fallback is reported.
        box = self._parsed()
        assert next(l for l in box.lines if l.mlb_person_id == 700002).is_starter
        assert "away:starter_from_appearance_order:700002" in box.anomalies

    def test_outs_parsed_from_innings_pitched_notation(self):
        # "5.2" is 5 innings + 2 outs = 17 outs, NOT five-point-two innings.
        box = self._parsed()
        line = next(l for l in box.lines if l.mlb_person_id == 700002)
        assert line.outs_recorded == 17

    def test_outs_stat_preferred_over_innings_pitched(self):
        box = self._parsed()
        assert next(l for l in box.lines if l.mlb_person_id == 608566).outs_recorded == 18

    def test_line_missing_denominators_is_dropped_and_reported(self):
        box = self._parsed()
        assert all(l.mlb_person_id != 700003 for l in box.lines)
        assert any(a.startswith("away:700003:missing:") for a in box.anomalies)

    def test_nullable_stats_stay_none_never_zero(self):
        box = self._parsed()
        reliever = next(l for l in box.lines if l.mlb_person_id == 700001)
        assert reliever.pitches_thrown is None
        assert reliever.sac_flies is None
        assert reliever.ground_outs == 3

    def test_pitch_hand_extracted_when_present_else_none(self):
        box = self._parsed()
        by_id = {l.mlb_person_id: l for l in box.lines}
        assert by_id[608566].pitch_hand == "R"
        assert by_id[700001].pitch_hand is None

    def test_missing_or_defaulted_counting_stats(self):
        box = self._parsed()
        starter = next(l for l in box.lines if l.mlb_person_id == 700002)
        # hitBatsmen omitted -> 0 (a real omission means none happened)...
        assert starter.hit_batsmen == 0
        assert starter.home_runs == 2
        # ...but fly balls omitted -> None (xFIP must know it's unrecorded).
        assert starter.fly_outs is None

    def test_two_games_started_flags_keep_first_in_appearance_order(self):
        payload = load_fixture("mlb_boxscore.json")
        home = payload["teams"]["home"]["players"]
        home["ID700001"]["stats"]["pitching"]["gamesStarted"] = 1
        box = parse_boxscore_pitching(payload)
        by_id = {l.mlb_person_id: l for l in box.lines if l.is_home}
        assert by_id[608566].is_starter is True
        assert by_id[700001].is_starter is False
        assert "home:starter_count:2" in box.anomalies

    def test_fallback_never_crowns_a_reliever(self):
        # The true starter's line is unparseable (empty pitching stats) and
        # no line carries gamesStarted: crowning the NEXT pitcher in
        # appearance order would flag a reliever as starter. Report no
        # starter instead.
        payload = load_fixture("mlb_boxscore.json")
        away = payload["teams"]["away"]["players"]
        away["ID700002"]["stats"]["pitching"] = {}
        box = parse_boxscore_pitching(payload)
        assert not any(l.is_starter for l in box.lines if not l.is_home)
        assert "away:starter_count:0" in box.anomalies

    def test_empty_payload_yields_no_lines_and_reports(self):
        box = parse_boxscore_pitching({"teams": {"home": {}, "away": {}}})
        assert box.lines == ()
        # No pitching data at all is not silently OK.
        assert "home:starter_count:0" in box.anomalies
        assert "away:starter_count:0" in box.anomalies
