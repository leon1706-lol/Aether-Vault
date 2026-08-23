"""Alembic migration chain for the av_server schema.

Startup runs this programmatically (av_server/database.py::init_db) — no alembic.ini
is required. Manual CLI usage from a checkout:

    python -m alembic -x sqlalchemy.url=postgresql+asyncpg://... upgrade head
"""
