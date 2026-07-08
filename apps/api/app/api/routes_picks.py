"""GET /api/v1/picks/* - typed stubs over the pick repository.

TODO(persistence): swap InMemoryPickRepository for the Postgres-backed
implementation (schema in infra/schema.sql) and populate it from the daily
cron scan.
"""

from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.picks.logger import InMemoryPickRepository, PickRecord, PickRepository

router = APIRouter(prefix="/api/v1/picks", tags=["picks"])

# Module-level singleton so all requests share the same (empty) store.
_repository = InMemoryPickRepository()


def get_repository() -> PickRepository:
    return _repository


class PickOut(BaseModel):
    """Public view of an audited pick."""

    pick_id: str
    created_at: datetime
    game_id: str
    sport: str
    market: str
    selection: str
    book: str
    taken_decimal_odds: float
    model_version: str
    p_model: float
    p_fair: float
    edge: float
    ev_per_unit: float
    stake_suggested: float
    clv_prob_pts: float | None
    beat_close: bool | None
    result: str | None

    @classmethod
    def from_record(cls, record: PickRecord) -> "PickOut":
        return cls(
            pick_id=record.pick_id,
            created_at=record.created_at,
            game_id=record.game_id,
            sport=record.sport,
            market=record.market,
            selection=record.selection,
            book=record.book,
            taken_decimal_odds=record.taken_decimal_odds,
            model_version=record.model_version,
            p_model=record.p_model,
            p_fair=record.p_fair,
            edge=record.edge,
            ev_per_unit=record.ev_per_unit,
            stake_suggested=record.stake_suggested,
            clv_prob_pts=record.clv_prob_pts,
            beat_close=record.beat_close,
            result=record.result,
        )


@router.get("/today", response_model=list[PickOut])
def picks_today(repo: PickRepository = Depends(get_repository)) -> list[PickOut]:
    """Picks published for today's slate (UTC date).

    TODO(cron): the daily scan should evaluate the whole slate and save picks
    that pass the edge/EV/calibration thresholds; until then this is empty.
    """
    return [PickOut.from_record(r) for r in repo.list_for_date(date.today())]


@router.get("/{pick_id}", response_model=PickOut)
def pick_by_id(pick_id: str, repo: PickRepository = Depends(get_repository)) -> PickOut:
    """Single pick with its audit fields, 404 if unknown."""
    record = repo.get(pick_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Pick {pick_id} not found")
    return PickOut.from_record(record)
