"""One-time data migration: SQLite (vernika.db) -> PostgreSQL.

Run INSIDE the backend container after the PostgreSQL-backed build is up:

    docker exec technopoliss-vernika python scripts/migrate_sqlite_to_postgres.py

It reads every table from the mounted SQLite file and copies it into the
PostgreSQL database (which must already have the schema created by init_db()).
Foreign-key triggers are disabled during the copy so insert order doesn't matter.
Sequences for ``BIGSERIAL`` id columns are reset so future auto-increments never
collide with migrated ids.
"""

from __future__ import annotations

import sqlite3
import sys


SQLITE_PATH = "/app/backend/data/vernika.db"

SKIP_TABLES = {"sqlite_sequence"}


def main() -> int:
    from core.db import PostgresConnection

    sq = sqlite3.connect(f"file:{SQLITE_PATH}?mode=ro", uri=True)
    sq.row_factory = sqlite3.Row

    pg = PostgresConnection()

    tables = [
        r[0]
        for r in sq.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    tables = [t for t in tables if t not in SKIP_TABLES and "fts" not in t.lower()]

    # Disable FK enforcement for the bulk copy (session-scoped).
    try:
        pg._conn.autocommit = True
        pg._conn.cursor().execute("SET session_replication_role = replica")
        pg._conn.autocommit = False
    except Exception as exc:
        print(f"WARN: could not disable FKs: {exc}")

    total = 0
    for t in tables:
        cols = [c[1] for c in sq.execute(f"PRAGMA table_info({t})").fetchall()]
        if not cols:
            continue
        rows = sq.execute(f'SELECT * FROM "{t}"').fetchall()
        if not rows:
            continue
        col_list = ", ".join(f'"{c}"' for c in cols)
        ph = ", ".join(["%s"] * len(cols))
        try:
            pg.execute(f'DELETE FROM "{t}"')
            for row in rows:
                vals = [row[c] for c in cols]
                pg.execute(f'INSERT INTO "{t}" ({col_list}) VALUES ({ph})', vals)
            pg.commit()
        except Exception as exc:
            # Skip tables that don't exist in the Postgres schema (legacy/orphaned
            # SQLite tables like campaign_states, or lazily-created dnc_list).
            pg.rollback()
            print(f"  SKIP {t}: {exc}")
            continue
        # Reset serial sequence when the table has an auto-increment id.
        if "id" in cols:
            try:
                pg.execute(
                    f"SELECT setval(pg_get_serial_sequence('{t}','id'), "
                    f"(SELECT COALESCE(MAX(id),1) FROM \"{t}\"), true)"
                )
                pg.commit()
            except Exception:
                pass
        total += len(rows)
        print(f"  migrated {t}: {len(rows)} rows")

    # Re-enable FK enforcement.
    try:
        pg._conn.autocommit = True
        pg._conn.cursor().execute("SET session_replication_role = origin")
        pg._conn.autocommit = False
    except Exception:
        pass

    sq.close()
    pg.close()
    print(f"DONE: {total} rows across {len(tables)} tables")
    return 0


if __name__ == "__main__":
    sys.exit(main())
