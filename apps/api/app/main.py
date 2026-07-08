"""EDGE API application entrypoint.

Run locally with: ``uvicorn app.main:app --reload --port 8000``
"""

from fastapi import FastAPI

from app.api.routes_analyze import router as analyze_router
from app.api.routes_performance import router as performance_router
from app.api.routes_picks import router as picks_router

app = FastAPI(
    title="EDGE API",
    version="0.1.0",
    description=(
        "Quantitative decision engine for sports betting (MVP: MLB moneyline "
        "and First-5-Innings moneyline). Informational only: it does not place "
        "bets, does not handle betting money and does not promise profit."
    ),
)

app.include_router(analyze_router)
app.include_router(picks_router)
app.include_router(performance_router)


@app.get("/health", tags=["ops"])
def health() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok"}
