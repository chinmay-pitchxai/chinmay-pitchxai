"""Test helpers — run the suite against the real production storage path.

The app's primary store is PostgreSQL (see docker-compose / core.db). These
helpers make every integration test talk to a dedicated ``technopoliss_test``
database so test runs never touch dev data, and they reset operational tables
per test class for deterministic assertions.

Run Postgres locally (matching docker-compose credentials)::

    docker run -d --name technopoliss-pg-local -e POSTGRES_DB=technopoliss \
        -e POSTGRES_USER=technopoliss -e POSTGRES_PASSWORD=technopoliss \
        -p 5432:5432 postgres:16-alpine
    docker exec technopoliss-pg-local psql -U technopoliss -d technopoliss \
        -c "CREATE DATABASE technopoliss_test"
"""

from __future__ import annotations

import os

# Must be set before the first PostgresConnection is opened (default_dsn reads
# the environment at connect time, so import order is safe either way).
os.environ.setdefault("PGDATABASE", "technopoliss_test")

from core.storage import _get_conn, new_db_connection  # noqa: E402

# Tables written by the E2E flows; truncated per test class for isolation.
OPERATIONAL_TABLES = (
    "workflow_jobs", "leads", "site_visits", "lead_memory", "feedback_records",
    "call_attempts", "do_not_contact", "whatsapp_messages", "scheduled_callbacks",
)


def connect():
    """Return a fresh Postgres-backed connection (sqlite3-compatible surface)."""
    return new_db_connection()


def reset_operational_tables(conn=None) -> None:
    """Delete all rows from operational tables (test isolation)."""
    own = conn is None
    conn = conn or _get_conn()
    try:
        for table in OPERATIONAL_TABLES:
            try:
                conn.execute(f"DELETE FROM {table}")
            except Exception:
                pass  # table may not exist in a minimal schema
        conn.commit()
    finally:
        if own:
            conn.close()
