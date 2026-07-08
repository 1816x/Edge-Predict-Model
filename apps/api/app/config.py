"""Application settings loaded from environment variables / `.env`.

Uses pydantic-settings. See `.env.example` for the expected variables.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the EDGE API."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- External services -------------------------------------------------
    # The Odds API v4 key (https://the-odds-api.com/liveapi/guides/v4/).
    odds_api_key: str = ""
    # SQLAlchemy URL, Postgres + psycopg 3 driver.
    database_url: str = "postgresql+psycopg://edge:edge@localhost:5432/edge"
    # LLM research/explanation layer key. The LLM never produces probabilities.
    llm_api_key: str = ""

    # --- Pick publication thresholds (MVP defaults, tunable) ----------------
    # Publish a pick only if edge >= edge_threshold AND ev >= ev_threshold
    # (and, once the model exists, ECE <= ece_threshold on a rolling window).
    edge_threshold: float = 0.02
    ev_threshold: float = 0.02
    ece_threshold: float = 0.03

    # --- Bankroll defaults (user-configurable in the SaaS layer) ------------
    default_kelly_user_fraction: float = 0.125  # Kelly/8
    default_stake_cap_pct: float = 0.02  # 2% of bankroll per pick


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (cheap to call from dependencies)."""
    return Settings()
