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

from sqlalchemy.exc import ProgrammingError

from app.config import get_settings
from app.db.engine import make_engine
from app.ml.dataset import (
    build_training_frame,
    load_market_prior,
    load_pitching_frame,
    load_results_frame,
)
from app.ml.train import MIN_GATE_N, walk_forward_report


def _sp_coverage(frame) -> float:
    """Share of rows where BOTH starters carry history-based features."""
    both = (
        frame["home_sp_kbb_pct_l5_starts"].notna()
        & frame["away_sp_kbb_pct_l5_starts"].notna()
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
    try:
        pitching = load_pitching_frame(engine)
        if len(pitching) == 0:
            pitching = None
            out["pitching_note"] = (
                "pitching_game_logs is empty; run backfill_pitching "
                "(sp_* features are all NaN this run)"
            )
    except ProgrammingError:
        # Table not there yet: the training report still runs on team form
        # alone and says so, instead of blocking on the migration.
        out["pitching_note"] = (
            "pitching tables missing; apply migration 003 and run "
            "backfill_pitching (sp_* features are all NaN this run)"
        )

    for market in markets:
        frame = build_training_frame(games, market, pitching)
        prior = load_market_prior(engine, market)
        frame = frame.merge(prior, on="event_id", how="left")
        out["markets"][market] = {
            "rows": int(len(frame)),
            "seasons": sorted(int(s) for s in frame["season"].unique()),
            # Share of rows with real starter features: after the full
            # backfill this should exceed ~0.95; lower means holes in the
            # pitching archive worth investigating before quoting metrics.
            "sp_coverage": _sp_coverage(frame),
            "rows_with_market_prior": int(frame["market_prior_p_home"].notna().sum()),
            "report": walk_forward_report(frame, min_train_seasons),
        }
    return out


def _markdown_summary(result: dict[str, Any]) -> str:
    lines = ["", "## Resumen F1 (walk-forward, calibrado con Platt)", ""]
    if "pitching_note" in result:
        lines.append(f"> ⚠ {result['pitching_note']}")
        lines.append("")
    for market, block in result.get("markets", {}).items():
        lines.append(
            f"### {market} — {block['rows']} juegos, temporadas {block['seasons']}, "
            f"sp_coverage {block['sp_coverage']}"
        )
        lines.append("")
        lines.append("| Test | n | const LL | home_rate LL | logistic LL | hist_gb LL | logistic ECE | hist_gb ECE |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for season, rep in sorted(block["report"]["seasons"].items()):
            lines.append(
                f"| {season} | {rep['logistic']['calibrated']['n']} "
                f"| {rep['baseline_constant']['log_loss']} "
                f"| {rep['baseline_home_rate']['log_loss']} "
                f"| {rep['logistic']['calibrated']['log_loss']} "
                f"| {rep['hist_gb']['calibrated']['log_loss']} "
                f"| {rep['logistic']['calibrated']['ece']} "
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
            lines.append("| Test | n_prior | prior LL | logistic LL | hist_gb LL | gate |")
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
                    f"| {sub['logistic_calibrated']['log_loss']} "
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
