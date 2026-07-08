"""Pick audit records and repository interface.

Every published pick must be auditable end to end (non-negotiable, see
docs/00-decisiones.md): the odds at pick time, the model version, the exact
feature snapshot the model saw, the closing line, the CLV and the graded
result. `PickRecord` carries all of that; `PickRepository` is the storage
interface. The Postgres implementation (SQLAlchemy, schema in
infra/schema.sql) is a TODO; `InMemoryPickRepository` backs tests and local
development.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Protocol


@dataclass
class PickRecord:
    """Full audit trail for a single published pick.

    Fields are grouped by lifecycle stage. Everything from `closing_*`
    onwards is filled in after the game closes/finishes, not at pick time.
    """

    # --- Identity ----------------------------------------------------------
    game_id: str  # provider game id (e.g. MLB gamePk as string)
    sport: str  # e.g. "baseball_mlb"
    market: str  # e.g. "moneyline" | "f5_moneyline"
    selection: str  # team/side picked, e.g. "SD Padres"
    pick_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # --- Price at pick time --------------------------------------------------
    book: str = ""  # book the taken price comes from
    taken_decimal_odds: float = 0.0  # decimal odds available when published
    reference_book: str = "pinnacle"  # no-vig reference line (MVP: Pinnacle)

    # --- Model output at pick time -------------------------------------------
    model_version: str = ""  # exact model artifact/version id
    p_model: float = 0.0  # calibrated model probability (NEVER from an LLM)
    p_fair: float = 0.0  # no-vig probability from the reference line
    edge: float = 0.0  # p_model - p_fair
    ev_per_unit: float = 0.0  # EV per 1 unit staked at taken odds
    features_snapshot: dict[str, Any] = field(default_factory=dict)  # as-of features

    # --- Stake suggestion ----------------------------------------------------
    kelly_full: float = 0.0
    user_fraction: float = 0.125
    cap_pct: float = 0.02
    stake_suggested: float = 0.0  # in bankroll currency units

    # --- Filled after close / final ------------------------------------------
    closing_decimal_odds: float | None = None  # reference book closing price (vigged)
    closing_fair_prob: float | None = None  # no-vig closing probability
    clv_prob_pts: float | None = None  # see app.core.clv convention
    beat_close: bool | None = None
    result: str | None = None  # "win" | "loss" | "push" | "void"
    profit_units: float | None = None  # net profit in units at taken odds


class PickRepository(Protocol):
    """Storage interface for pick audit records."""

    def save(self, pick: PickRecord) -> PickRecord:
        """Persist a pick (insert or update by pick_id) and return it."""
        ...

    def get(self, pick_id: str) -> PickRecord | None:
        """Return the pick with this id, or None if unknown."""
        ...

    def list_for_date(self, day: date) -> list[PickRecord]:
        """Return picks created on the given UTC date."""
        ...


class InMemoryPickRepository:
    """Dict-backed PickRepository for tests and local development.

    TODO: replace with a SQLAlchemy/Postgres implementation persisting to the
    schema in infra/schema.sql.
    """

    def __init__(self) -> None:
        self._picks: dict[str, PickRecord] = {}

    def save(self, pick: PickRecord) -> PickRecord:
        self._picks[pick.pick_id] = pick
        return pick

    def get(self, pick_id: str) -> PickRecord | None:
        return self._picks.get(pick_id)

    def list_for_date(self, day: date) -> list[PickRecord]:
        return [p for p in self._picks.values() if p.created_at.date() == day]
