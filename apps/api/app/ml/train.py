"""Walk-forward training, Platt calibration and honest metrics.

Temporal discipline (docs/06): for each test season S, models fit on seasons
strictly before S, the calibrator fits on the LAST training season only
(out-of-time for the fit set, never touching S), and S is scored once. No
shuffling anywhere.

Baselines:
- ``constant``: p = 0.5 for every game.
- ``home_rate``: expanding home-win rate using only games strictly before
  each test game (leak-free running mean).
- ``market_prior`` (the REAL gate of docs/04 §2.4) is NOT computable yet:
  it needs historical odds, which the free tier does not provide. Until the
  own-snapshot archive matures (or a paid historical backfill), model-vs-
  market claims are out of reach and this report says so explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression

from app.ml.dataset import FEATURE_COLUMNS

MIN_TRAIN_SEASONS = 4
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
    frame: pd.DataFrame, medians: pd.Series | None = None
) -> tuple[np.ndarray, pd.Series]:
    """Median-impute NaNs (medians learned on train only — no leakage)."""
    x = frame[FEATURE_COLUMNS]
    if medians is None:
        medians = x.median()
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
    frame: pd.DataFrame, min_train_seasons: int = MIN_TRAIN_SEASONS
) -> dict[str, Any]:
    """Train/evaluate per test season; returns nested metrics."""
    seasons = sorted(frame["season"].unique())
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

        x_fit, medians = _prep_matrix(frame.loc[fit_mask])
        x_calib, _ = _prep_matrix(frame.loc[calib_mask], medians)
        x_test, _ = _prep_matrix(frame.loc[test_mask], medians)

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
            "hist_gb": HistGradientBoostingClassifier(
                max_depth=3, learning_rate=0.05, max_iter=300,
                l2_regularization=1.0, random_state=7,
            ),
        }
        for name, model in models.items():
            model.fit(x_fit, y_fit)
            calibrator = PlattCalibrator.fit(model.predict_proba(x_calib)[:, 1], y_calib)
            p_test_raw = model.predict_proba(x_test)[:, 1]
            season_report[name] = {
                "raw": _metrics(y_test, p_test_raw),
                "calibrated": _metrics(y_test, calibrator.apply(p_test_raw)),
            }

        report["seasons"][int(test_season)] = season_report

    return report
