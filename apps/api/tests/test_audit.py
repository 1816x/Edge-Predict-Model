"""audit_snapshots: pure gap detection + job integration."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.jobs.audit_snapshots import find_gaps

START = datetime(2026, 7, 8, 23, 0, tzinfo=timezone.utc)
MAX_GAP = timedelta(hours=4)


def _t(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 7, 8, hour, minute, tzinfo=timezone.utc)


class TestFindGaps:
    def test_zero_captures_is_one_full_window_hole(self):
        assert find_gaps(START, [], MAX_GAP) == [(START - timedelta(hours=10), START)]

    def test_regular_cadence_is_clean(self):
        # 15:23 / 18:23 / 21:23 snapshots, start 23:00 -> max spacing 3h.
        assert find_gaps(START, [_t(15, 23), _t(18, 23), _t(21, 23)], MAX_GAP) == []

    def test_missed_middle_run_is_a_hole(self):
        # 18:23 missing: 15:23 -> 21:23 is 6h.
        gaps = find_gaps(START, [_t(15, 23), _t(21, 23)], MAX_GAP)
        assert gaps == [(_t(15, 23), _t(21, 23))]

    def test_stale_last_capture_is_a_trailing_hole(self):
        # Nothing between 18:00 and first pitch at 23:00.
        gaps = find_gaps(START, [_t(15), _t(18)], MAX_GAP)
        assert gaps == [(_t(18), START)]

    def test_leading_edge_is_not_a_hole(self):
        # First capture 9h into the 10h window: early games meet fewer cron
        # slots — cadence design, not a missed run.
        assert find_gaps(START, [_t(22), _t(22, 30)], MAX_GAP) == []

    def test_captures_outside_window_are_ignored_and_deduped(self):
        captures = [
            _t(10),  # 13h before start: outside the 10h window
            datetime(2026, 7, 9, 1, 0, tzinfo=timezone.utc),  # after start
            _t(15, 23), _t(15, 23),  # duplicate
            _t(18, 23), _t(21, 23),
        ]
        assert find_gaps(START, captures, MAX_GAP) == []


@pytest.mark.integration
def test_audit_reports_days_and_gapped_events(db):
    from datetime import datetime as dt

    from app.ingestion import store
    from app.ingestion.parsers import ScheduledGame
    from app.jobs import audit_snapshots

    tables = store.reflect_tables(db)

    def _seed_event(conn, sport_id, pk, start):
        event_id, _ = store.upsert_event_from_schedule(
            conn, tables, sport_id,
            ScheduledGame(
                game_pk=pk, start_time=start, status="final",
                home_name="Boston Red Sox", away_name="New York Yankees",
                home_mlb_id=None, away_mlb_id=None,
                home_probable=None, away_probable=None,
            ),
        )
        return event_id

    def _snap(conn, event_id, captured_at):
        conn.execute(
            text(
                """
                INSERT INTO odds_snapshots
                    (event_id, book_id, market, side, price_decimal,
                     price_american, captured_at)
                SELECT :event_id, id, 'moneyline', 'home', 1.91, -110, :captured_at
                FROM books WHERE key = 'pinnacle'
                """
            ),
            {"event_id": event_id, "captured_at": captured_at},
        )

    with db.begin() as conn:
        sport_id = store.get_sport_id(conn, tables)
        # Clean: 3 captures, none more than 4h apart through first pitch.
        clean = _seed_event(conn, sport_id, 920001, START)
        for hour in (15, 18, 21):
            _snap(conn, clean, _t(hour, 23))
        # Gapped: one early capture, then nothing until a 20:00 start.
        gapped = _seed_event(
            conn, sport_id, 920002, dt(2026, 7, 8, 20, 0, tzinfo=timezone.utc)
        )
        _snap(conn, gapped, _t(10, 23))
        # Started with zero snapshots.
        _seed_event(conn, sport_id, 920003, dt(2026, 7, 8, 17, 0, tzinfo=timezone.utc))
        # Future game: excluded from the event audit (window incomplete).
        _seed_event(conn, sport_id, 920004, dt(2026, 7, 9, 18, 0, tzinfo=timezone.utc))

    result = audit_snapshots.run(
        "2026-07-08", "2026-07-09", engine=db,
        now=dt(2026, 7, 9, 12, 0, tzinfo=timezone.utc),
    )
    assert result["events_audited"] == 3
    assert result["events_clean"] == 1
    assert result["events_with_gaps"] == 2
    assert result["events_zero_snapshots"] == 1
    assert {e["mlb_game_pk"] for e in result["gapped_events"]} == {"920002", "920003"}

    by_day = {d["day"]: d for d in result["days"]}
    assert by_day["2026-07-08"]["events"] == 3
    assert by_day["2026-07-08"]["events_with_pregame"] == 2
    assert by_day["2026-07-08"]["capture_runs"] == 4  # 15:23, 18:23, 21:23, 10:23
    assert by_day["2026-07-09"]["events"] == 1

    import json

    json.dumps(result)
