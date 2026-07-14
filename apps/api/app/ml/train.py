"""Walk-forward training, Platt calibration and honest metrics.

Temporal discipline (docs/06): for each test season S, models fit on seasons
strictly before S, the calibrator fits on the LAST training season only
(out-of-time for the fit set, never touching S), and S is scored once. No
shuffling anywhere.

Baselines:
- ``constant``: p = 0.5 for every game.
- ``home_rate``: expanding home-win rate using only games strictly before
  each test game (leak-free running mean).
- ``market_prior`` (the REAL gate of docs/04 §2.4): computable only for
  games with a pregame sharp-book snapshot in the own archive, which
  started 2026-07-08 — so only on a growing SUBSET of recent games. When
  the frame carries ``market_prior_p_home``, each test season reports the
  prior's metrics AND every model's metrics over that same subset (never
  compare metrics across different rows). Below MIN_GATE_N evaluable games
  the gate is explicitly NOT evaluated: a conclusion from 30 games would
  be noise wearing a suit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from app.ml.dataset import FEATURE_COLUMNS

MIN_TRAIN_SEASONS = 4
# Minimum games with an archived pregame prior before the docs/04 §2.4 gate
# (model log loss < market prior log loss) is worth evaluating at all.
MIN_GATE_N = 200
_EPS = 1e-6


def _logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(p, _EPS, 1 - _EPS)
    return np.log(p / (1 - p))


@dataclass
class PlattCalibrator:
    """1-D logistic fit on the raw model's log-odds (docs/04 §3: Platt)."""

    inner: LogisticRegression

    @classmethod
    def fit(cls, p_raw: np.ndarray, y: np.ndarray) -> "PlattCalibrator":
        lr = LogisticRegression(C=1e6, max_iter=1000)
        lr.fit(_logit(p_raw).reshape(-1, 1), y)
        return cls(lr)

    def apply(self, p_raw: np.ndarray) -> np.ndarray:
        return self.inner.predict_proba(_logit(p_raw).reshape(-1, 1))[:, 1]


def log_loss(y: np.ndarray, p: np.ndarray) -> float:
    p = np.clip(p, _EPS, 1 - _EPS)
    return float(-(y * np.log(p) + (1 - y) * np.log(1 - p)).mean())


def brier(y: np.ndarray, p: np.ndarray) -> float:
    return float(((p - y) ** 2).mean())


def ece(y: np.ndarray, p: np.ndarray, bins: int = 10) -> float:
    """Expected calibration error, equal-width bins (docs/06 definition)."""
    edges = np.linspace(0.0, 1.0, bins + 1)
    total = 0.0
    for lo, hi in zip(edges[:-1], edges[1:]):
        mask = (p >= lo) & (p < hi) if hi < 1.0 else (p >= lo) & (p <= hi)
        if mask.sum() == 0:
            continue
        total += (mask.sum() / len(p)) * abs(p[mask].mean() - y[mask].mean())
    return float(total)


def _metrics(y: np.ndarray, p: np.ndarray) -> dict[str, float]:
    return {
        "n": int(len(y)),
        "log_loss": round(log_loss(y, p), 5),
        "brier": round(brier(y, p), 5),
        "ece": round(ece(y, p), 5),
    }


def _prep_matrix(
    frame: pd.DataFrame,
    columns: list[str],
    medians: pd.Series | None = None,
) -> tuple[np.ndarray, pd.Series]:
    """Median-impute NaNs (medians learned on train only — no leakage).

    A column that is 100% NaN on the training window (e.g. the sp_* block
    before the pitching backfill runs) has NaN median; it falls back to 0.0
    so sklearn still fits — the report's sp_coverage exposes the situation.
    """
    x = frame[columns]
    if medians is None:
        medians = x.median().fillna(0.0)
    return x.fillna(medians).to_numpy(dtype=float), medians


def _expanding_home_rate(frame: pd.DataFrame) -> np.ndarray:
    """Leak-free running home-win rate: mean of targets strictly before row i."""
    y = frame["target"].to_numpy(dtype=float)
    cum = np.concatenate([[0.0], np.cumsum(y)])[:-1]
    counts = np.arange(len(y), dtype=float)
    with np.errstate(invalid="ignore", divide="ignore"):
        rate = np.where(counts > 0, cum / counts, 0.5)
    return rate


def walk_forward_report(
    frame: pd.DataFrame,
    min_train_seasons: int = MIN_TRAIN_SEASONS,
    feature_columns: list[str] | None = None,
) -> dict[str, Any]:
    """Train/evaluate per test season; returns nested metrics.

    ``feature_columns`` selects the vector (markets differ: F5 excludes the
    bullpen block). Default: every FEATURE_COLUMNS entry present in the
    frame, in canonical order — so a frame built for either market trains
    on exactly its own columns.
    """
    columns = (
        list(feature_columns)
        if feature_columns is not None
        else [c for c in FEATURE_COLUMNS if c in frame.columns]
    )
    # Plain ints: numpy int32 seasons would survive into the report and
    # break json.dumps at the very end of a long training run.
    seasons = sorted(int(s) for s in frame["season"].unique())
    home_rate = _expanding_home_rate(frame)
    report: dict[str, Any] = {"seasons": {}, "n_total": int(len(frame))}

    for test_season in seasons:
        train_seasons = [s for s in seasons if s < test_season]
        if len(train_seasons) < min_train_seasons:
            continue
        calib_season = train_seasons[-1]
        fit_mask = frame["season"] < calib_season
        calib_mask = frame["season"] == calib_season
        test_mask = frame["season"] == test_season
        y_fit = frame.loc[fit_mask, "target"].to_numpy()
        y_calib = frame.loc[calib_mask, "target"].to_numpy()
        y_test = frame.loc[test_mask, "target"].to_numpy()
        if len(y_test) == 0:
            continue

        x_fit, medians = _prep_matrix(frame.loc[fit_mask], columns)
        x_calib, _ = _prep_matrix(frame.loc[calib_mask], columns, medians)
        x_test, _ = _prep_matrix(frame.loc[test_mask], columns, medians)

        season_report: dict[str, Any] = {
            "train_seasons": train_seasons,
            "calibration_season": calib_season,
            "baseline_constant": _metrics(y_test, np.full(len(y_test), 0.5)),
            "baseline_home_rate": _metrics(
                y_test, home_rate[test_mask.to_numpy()]
            ),
        }

        models = {
            "logistic": LogisticRegression(max_iter=2000, C=1.0),
            # docs/04 §2.2 mandates standardized features for the logistic;
            # the Pipeline fits the scaler on THIS fold's train only (never
            # the full dataset — that would leak future means/variances).
            # The unscaled twin stays for ONE more tanda as the attribution
            # witness vs the pre-offense baseline (run #37); drop it once
            # the F1.2 measurement is recorded in PLAN.md.
            "logistic_scaled": make_pipeline(
                StandardScaler(), LogisticRegression(max_iter=2000, C=1.0)
            ),
            "hist_gb": HistGradientBoostingClassifier(
                max_depth=3, learning_rate=0.05, max_iter=300,
                l2_regularization=1.0, random_state=7,
            ),
        }
        p_calibrated: dict[str, np.ndarray] = {}
        for name, model in models.items():
            model.fit(x_fit, y_fit)
            calibrator = PlattCalibrator.fit(model.predict_proba(x_calib)[:, 1], y_calib)
            p_test_raw = model.predict_proba(x_test)[:, 1]
            p_calibrated[name] = calibrator.apply(p_test_raw)
            season_report[name] = {
                "raw": _metrics(y_test, p_test_raw),
                "calibrated": _metrics(y_test, p_calibrated[name]),
            }

        if "market_prior_p_home" in frame.columns:
            season_report["market_prior_subset"] = _market_prior_subset(
                frame.loc[test_mask, "market_prior_p_home"].to_numpy(dtype=float),
                y_test,
                p_calibrated,
            )

        report["seasons"][int(test_season)] = season_report

    return report


def _market_prior_subset(
    prior: np.ndarray, y_test: np.ndarray, p_calibrated: dict[str, np.ndarray]
) -> dict[str, Any]:
    """docs/04 §2.4 gate material: prior AND models scored on the SAME rows.

    Comparing a model's full-season log loss against the prior's subset log
    loss would be apples to oranges; every number here uses only the games
    that actually have an archived pregame prior.
    """
    mask = ~np.isnan(prior)
    n = int(mask.sum())
    out: dict[str, Any] = {"n": n, "min_gate_n": MIN_GATE_N}
    if n == 0:
        out["note"] = "no games with archived pregame sharp odds in this season"
        return out
    y_sub = y_test[mask]
    out["market_prior"] = _metrics(y_sub, prior[mask])
    for name, p in p_calibrated.items():
        out[f"{name}_calibrated"] = _metrics(y_sub, p[mask])
    if n < MIN_GATE_N:
        out["gate"] = {
            "evaluated": False,
            "note": (
                f"insufficient sample (n={n} < {MIN_GATE_N}); gate NOT "
                "evaluated — publishing stays blocked"
            ),
        }
    else:
        out["gate"] = {
            "evaluated": True,
            "beaten_by": {
                name: out[f"{name}_calibrated"]["log_loss"]
                < out["market_prior"]["log_loss"]
                for name in p_calibrated
            },
        }
    return out
