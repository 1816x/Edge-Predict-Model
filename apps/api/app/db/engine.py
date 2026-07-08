"""SQLAlchemy engine factory.

The schema's single source of truth is ``infra/schema.sql`` (applied with
psql). Python code reflects tables from the live database instead of
duplicating their definitions as ORM models, so any drift between code and
schema fails loudly at reflection time rather than silently at query time.
"""

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine


def make_engine(database_url: str) -> Engine:
    """Create an engine with pre-ping (cron jobs may reuse stale pools)."""
    return create_engine(database_url, pool_pre_ping=True)
