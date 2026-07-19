"""F1 training/evaluation job: walk-forward metrics report (read-only).

Usage::

    python -m app.jobs.train_f1 [--markets moneyline,f5_moneyline] [--min-train-seasons 4]

Loads finished games from the database, builds the as-of team-form dataset,
trains logistic + gradient-boosting models walk-forward by season with Platt
calibration, and prints a JSON report plus a readable markdown summary.

HONEST LIMITATION (read before quoting numbers): the hard publication gate
of docs/04 §2.4 is beating the MARKET PRIOR's log loss, and that needs
historical odds we do not have yet (free tier; own snapshots started
2026-07-08). Until then this report only shows whether the models carry
signal vs naive baselines and whether calibration holds. It does NOT
authorize publishing picks.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from pandas.errors import DatabaseError
from sqlalchemy.exc import ProgrammingError

from app.config import get_settings
from app.db.engine import make_engine
from app.ml.dataset import (
    build_training_frame,
    feature_columns,
    load_batting_frame,
    load_bullpen_frame,
    load_lineup_frame,
    load_market_prior,
    load_pitching_frame,
    load_results_frame,
    load_transactions_frame,
)
from app.ml.train import MIN_GATE_N, walk_forward_report


def _sp_coverage(frame) -> float:
    """Share of rows where BOTH starters carry history-based features."""
    both = (
        frame["home_sp_kbb_pct_l5_starts"].notna()
        & frame["away_sp_kbb_pct_l5_starts"].notna()
    )
    return round(float(both.mean()), 4) if len(frame) else 0.0


def _bullpen_coverage(frame) -> float:
    """Share of rows with a live reliever archive (fatigue features real)."""
    both = (
        frame["home_bullpen_ip_l3d"].notna() & frame["away_bullpen_ip_l3d"].notna()
    )
    return round(float(both.mean()), 4) if len(frame) else 0.0


def _bullpen_il_coverage(frame) -> float:
    """Share of rows where BOTH teams carry a real bullpen_il_depletion
    (docs/04 §1.4b, Moneyline only).

    Lower than bullpen coverage BY DESIGN: the count is None until THREE gates
    hold as-of — the reliever archive is alive, the transactions archive is
    alive, and the team has an established (>=3 IP in 30d) quality arm to rank —
    so a hole never reads as a fabricated 0."""
    both = (
        frame["home_bullpen_il_depletion"].notna()
        & frame["away_bullpen_il_depletion"].notna()
    )
    return round(float(both.mean()), 4) if len(frame) else 0.0


def _offense_coverage(frame) -> float:
    """Share of rows where BOTH teams carry a real 30d offense window."""
    both = frame["home_team_woba_30d"].notna() & frame["away_team_woba_30d"].notna()
    return round(float(both.mean()), 4) if len(frame) else 0.0


def _lineup_coverage(frame) -> float:
    """Share of rows where BOTH lineups carry a real projected wOBA."""
    both = (
        frame["home_lineup_woba_proj"].notna()
        & frame["away_lineup_woba_proj"].notna()
    )
    return round(float(both.mean()), 4) if len(frame) else 0.0


def _star_out_coverage(frame) -> float:
    """Share of rows where BOTH teams carry a real star_out_flag (docs/04 §1.5).

    Lower than lineup coverage by design: the flag is None until the
    transactions archive is alive as-of AND the team has an established (>=200
    PA) star to speak of — early April and thin rosters legitimately have no
    star, and a hole must never read as a fabricated 0."""
    both = (
        frame["home_star_out_flag"].notna() & frame["away_star_out_flag"].notna()
    )
    return round(float(both.mean()), 4) if len(frame) else 0.0


def run(
    markets: tuple[str, ...] = ("moneyline", "f5_moneyline"),
    min_train_seasons: int = 4,
    *,
    engine=None,
) -> dict[str, Any]:
    engine = engine or make_engine(get_settings().database_url)
    games = load_results_frame(engine)
    out: dict[str, Any] = {
        "job": "train_f1",
        "games_with_results": int(len(games)),
        "markets": {},
        "gate_note": (
            "docs/04 §2.4 gate (log loss < market prior) is evaluated ONLY "
            "over games with archived pregame sharp odds (own archive, "
            f"started 2026-07-08) and only once that subset reaches n>={MIN_GATE_N} "
            "per test season — see market_prior_subset. Until the gate is "
            "evaluated AND beaten, publishing stays blocked"
        ),
    }
    if len(games) == 0:
        out["error"] = "no finished games with results; run backfill_results first"
        return out

    pitching = None
    bullpen = None
    try:
        pitching = load_pitching_frame(engine)
        bullpen = load_bullpen_frame(engine)
        # Independent emptiness checks: starter and reliever coverage are
        # separate archives from the model's point of view — never let one
        # side's hole silently degrade the other's features.
        if len(pitching) == 0:
            pitching = None
            out["pitching_note"] = (
                "pitching_game_logs has no starter lines; run "
                "backfill_pitching (sp_* features are all NaN this run)"
            )
        if len(bullpen) == 0:
            bullpen = None
            out["bullpen_note"] = (
                "pitching_game_logs has no reliever lines; run "
                "backfill_pitching (bullpen_* features are all NaN this run)"
            )
    except (ProgrammingError, DatabaseError):
        # Table not there yet (pandas wraps the driver error in its own
        # DatabaseError): the training report still runs on team form alone
        # and says so, instead of blocking on the migration.
        out["pitching_note"] = (
            "pitching tables missing; apply migration 003 and run "
            "backfill_pitching (sp_*/bullpen_* features are all NaN this run)"
        )

    # Batting has its own try (migration 004 is newer than 003): a database
    # in the 003-but-not-004 state must still train with real sp/bullpen
    # features and only degrade the offense block.
    batting = None
    lineup = None
    try:
        batting = load_batting_frame(engine)
        # Same table as batting (migration 004): the lineup block reads the
        # realized batting_order for backtest composition, so it lives or
        # dies with the batting archive, not with event_lineups (that table
        # only feeds the ONLINE builder going forward).
        lineup = load_lineup_frame(engine)
        if len(batting) == 0:
            batting = None
            out["batting_note"] = (
                "batting_game_logs is empty; run backfill_pitching "
                "(team offense and lineup features are all NaN this run)"
            )
        if lineup is not None and len(lineup) == 0:
            lineup = None
    except (ProgrammingError, DatabaseError):
        out["batting_note"] = (
            "batting_game_logs missing; apply migration 004 and run "
            "backfill_pitching (team offense and lineup features are all NaN)"
        )

    # Transactions/IL archive has its own try (migration 006 is newer): pre-006
    # the report still trains, only star_out_flag and bullpen_il_depletion
    # degrade to NaN.
    transactions = None
    try:
        transactions = load_transactions_frame(engine)
        if len(transactions) == 0:
            transactions = None
    except (ProgrammingError, DatabaseError):
        out["transactions_note"] = (
            "player_transactions missing; apply migration 006 and run "
            "sync_transactions (star_out_flag and bullpen_il_depletion are all "
            "NaN this run)"
        )

    for market in markets:
        frame = build_training_frame(
            games, market, pitching, bullpen, batting, lineup, transactions
        )
        prior = load_market_prior(engine, market)
        frame = frame.merge(prior, on="event_id", how="left")
        block = {
            "rows": int(len(frame)),
            "seasons": sorted(int(s) for s in frame["season"].unique()),
            # Share of rows with real starter features: after the full
            # backfill this should exceed ~0.95; lower means holes in the
            # pitching archive worth investigating before quoting metrics.
            "sp_coverage": _sp_coverage(frame),
            # Same idea for the offense block (both markets carry it).
            "offense_coverage": _offense_coverage(frame),
            # Lineup block coverage: 0 until the batting backfill fills the
            # realized batting_order (both markets carry it).
            "lineup_coverage": _lineup_coverage(frame),
            # star_out_flag coverage (both markets): fraction with an alive IL
            # archive AND an identifiable star; lower than lineup by design.
            "star_out_coverage": _star_out_coverage(frame),
            "rows_with_market_prior": int(frame["market_prior_p_home"].notna().sum()),
            "report": walk_forward_report(
                frame, min_train_seasons, feature_columns(market)
            ),
        }
        if market == "moneyline":
            block["bullpen_coverage"] = _bullpen_coverage(frame)
            # bullpen_il_depletion coverage (§1.4b, Moneyline only): fraction
            # with the reliever archive, the IL archive AND a rankable arm all
            # live as-of; lower than bullpen_coverage by design.
            block["bullpen_il_coverage"] = _bullpen_il_coverage(frame)
        out["markets"][market] = block
    return out


def _markdown_summary(result: dict[str, Any]) -> str:
    lines = ["", "## Resumen F1 (walk-forward, calibrado con Platt)", ""]
    for note_key in ("pitching_note", "bullpen_note", "batting_note"):
        if note_key in result:
            lines.append(f"> ⚠ {result[note_key]}")
            lines.append("")
    for market, block in result.get("markets", {}).items():
        coverage = (
            f"sp_coverage {block['sp_coverage']}, "
            f"offense_coverage {block['offense_coverage']}, "
            f"lineup_coverage {block['lineup_coverage']}, "
            f"star_out_coverage {block['star_out_coverage']}"
        )
        if "bullpen_coverage" in block:
            coverage += f", bullpen_coverage {block['bullpen_coverage']}"
        if "bullpen_il_coverage" in block:
            coverage += f", bullpen_il_coverage {block['bullpen_il_coverage']}"
        lines.append(
            f"### {market} — {block['rows']} juegos, temporadas {block['seasons']}, "
            f"{coverage}"
        )
        lines.append("")
        lines.append("| Test | n | const LL | home_rate LL | log_scaled LL | hist_gb LL | log_scaled ECE | hist_gb ECE |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for season, rep in sorted(block["report"]["seasons"].items()):
            lines.append(
                f"| {season} | {rep['logistic_scaled']['calibrated']['n']} "
                f"| {rep['baseline_constant']['log_loss']} "
                f"| {rep['baseline_home_rate']['log_loss']} "
                f"| {rep['logistic_scaled']['calibrated']['log_loss']} "
                f"| {rep['hist_gb']['calibrated']['log_loss']} "
                f"| {rep['logistic_scaled']['calibrated']['ece']} "
                f"| {rep['hist_gb']['calibrated']['ece']} |"
            )
        lines.append("")
        prior_rows = [
            (season, rep["market_prior_subset"])
            for season, rep in sorted(block["report"]["seasons"].items())
            if rep.get("market_prior_subset", {}).get("n", 0) > 0
        ]
        if prior_rows:
            lines.append(
                "Subconjunto con market prior archivado (mismas filas para prior y modelos):"
            )
            lines.append("")
            lines.append("| Test | n_prior | prior LL | log_scaled LL | hist_gb LL | gate |")
            lines.append("|---|---|---|---|---|---|")
            for season, sub in prior_rows:
                gate = sub.get("gate", {})
                verdict = (
                    str(gate.get("beaten_by"))
                    if gate.get("evaluated")
                    else f"no evaluado (n<{sub['min_gate_n']})"
                )
                lines.append(
                    f"| {season} | {sub['n']} "
                    f"| {sub['market_prior']['log_loss']} "
                    f"| {sub['logistic_scaled_calibrated']['log_loss']} "
                    f"| {sub['hist_gb_calibrated']['log_loss']} "
                    f"| {verdict} |"
                )
            lines.append("")
        else:
            lines.append(
                f"_Sin juegos con market prior archivado aún "
                f"(rows_with_market_prior={block['rows_with_market_prior']})._"
            )
            lines.append("")
    lines.append(f"> {result['gate_note']}")
    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--markets", default="moneyline,f5_moneyline")
    parser.add_argument("--min-train-seasons", type=int, default=4)
    args = parser.parse_args()
    result = run(
        markets=tuple(m.strip() for m in args.markets.split(",") if m.strip()),
        min_train_seasons=args.min_train_seasons,
    )
    print(json.dumps(result))
    print(_markdown_summary(result))
