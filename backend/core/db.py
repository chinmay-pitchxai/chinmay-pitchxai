"""PostgreSQL storage adapter — a sqlite3-API-compatible shim.

The Technopolis backend was written against sqlite3: ``core.storage._get_conn()``
returns a connection whose API is used by ~200 call sites across the codebase
(``execute``/``executescript``/``commit``/``rollback``/``close``/``row_factory``/
``fetchone``/``fetchall``/``rowcount``/``lastrowid``). This module provides a
Postgres-backed connection that exposes that *same* surface while translating
SQLite SQL -> PostgreSQL SQL on the fly, so "PostgreSQL only" is a swap of the
connection layer rather than a rewrite of every query.

Dialect translations handled:
  - ``?`` placeholders            ->  ``%s`` (skipping ``?`` inside string literals)
  - ``INSERT OR IGNORE ...``      ->  ``INSERT ... ON CONFLICT DO NOTHING``
  - ``INSERT OR REPLACE INTO t(a,b)`` ->  ``INSERT INTO t(a,b) ... ON CONFLICT(a) DO UPDATE SET b=EXCLUDED.b``
  - ``datetime('now')``           ->  ``now()``
  - ``ALTER TABLE ... ADD COLUMN x`` -> ``ADD COLUMN IF NOT EXISTS x`` (idempotent)
  - ``row_factory = sqlite3.Row`` ->  dict-like rows (indexable by name AND position)
  - ``cursor.lastrowid``          ->  ``INSERT ... RETURNING id`` for serial-id tables
  - sqlite3 harmless errors ("duplicate column", "no such table", ...) are
    re-raised as ``sqlite3.OperationalError`` so existing ``except`` blocks work.

Connection settings come from the environment (psycopg2-style): ``PGHOST``,
``PGPORT``, ``PGDATABASE``, ``PGUSER``, ``PGPASSWORD``, or a single ``DATABASE_URL``.
"""

from __future__ import annotations

import os
import re
import sqlite3  # noqa: F401  (re-exported so callers' `sqlite3.Row`/`sqlite3.OperationalError` still resolve)
import threading
import time
from typing import Any

import psycopg2
import psycopg2.extras
from psycopg2 import errors as pg_errors


# ── Tables whose primary key is an auto-incrementing integer column ``id`` ──
# ``cursor.lastrowid`` is only meaningful for these; for INSERTs into them we
# append ``RETURNING id`` and capture the value.
_SERIAL_ID_TABLES = frozenset({
    "leads", "workflow_jobs", "site_visits", "feedback_records",
    "whatsapp_messages", "call_attempts", "cases", "schedules",
    "manual_calls", "incoming_calls", "prompt_versions", "agent_leads",
    "conversation_messages", "virtual_meets", "scheduled_callbacks",
    "call_logs",
})


# ── SQL translation ────────────────────────────────────────────────────────

def _replace_placeholders(sql: str) -> str:
    """Replace ``?`` with ``%s``, skipping ``?`` inside single-quoted strings."""
    out: list[str] = []
    in_str = False
    i = 0
    n = len(sql)
    while i < n:
        c = sql[i]
        if c == "'":
            if in_str and i + 1 < n and sql[i + 1] == "'":
                out.append("''")
                i += 2
                continue
            in_str = not in_str
            out.append(c)
            i += 1
            continue
        if c == "?" and not in_str:
            out.append("%s")
            i += 1
            continue
        if c == "%":
            nxt = sql[i + 1] if i + 1 < n else ""
            out.append(c if nxt in ("s", "%", "(") else "%%")
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


_INSERT_OR_IGNORE_RE = re.compile(r"(?is)\bINSERT\s+OR\s+IGNORE\s+INTO\b")
_INSERT_OR_REPLACE_RE = re.compile(
    r"(?is)\bINSERT\s+OR\s+REPLACE\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)"
)
_ALTER_ADD_COLUMN_RE = re.compile(
    r"(?is)\bALTER\s+TABLE\s+([A-Za-z_][A-Za-z0-9_]*)\s+ADD\s+COLUMN\s+"
    r"([A-Za-z_][A-Za-z0-9_]*)(?!\s+IF\s+NOT\s+EXISTS)"
)

# SQLite json_extract(col, '$.a.b') -> (col::jsonb->>'a') / (col::jsonb#>>'{a,b}')
_JSON_EXTRACT_RE = re.compile(
    r"(?is)json_extract\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*,\s*'\$\.([A-Za-z0-9_.]+)'\s*\)"
)
# SQLite date(expr) -> (expr)::date  (columns here are TEXT timestamps)
_DATE_FN_RE = re.compile(r"(?i)\bdate\s*\(\s*([^()]+?)\s*\)")
# datetime('now', MOD) -> now() + (MOD)::interval
_DATETIME_NOW_MOD_RE = re.compile(r"(?i)datetime\('now',\s*([^)]+)\)")
# datetime(COL, 'unixepoch', ...) -> to_timestamp(COL)
_DATETIME_UNIX_RE = re.compile(r"(?i)\bdatetime\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*,\s*'unixepoch'[^)]*\)")
# datetime(COL) -> (COL)::timestamp
_DATETIME_COL_RE = re.compile(r"(?i)\bdatetime\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)")


def _json_extract_repl(m: re.Match) -> str:
    col, path = m.group(1), m.group(2)
    parts = path.split(".")
    if len(parts) == 1:
        return f"({col}::jsonb->>'{parts[0]}')"
    return f"({col}::jsonb#>>'{{{','.join(parts)}}}')"


def _translate_ddl_types(sql: str) -> str:
    """Map SQLite column types in CREATE TABLE statements to PostgreSQL."""
    sql = re.sub(r"(?i)INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT", "BIGSERIAL PRIMARY KEY", sql)
    sql = re.sub(r"(?i)INTEGER\s+PRIMARY\s+KEY", "BIGINT PRIMARY KEY", sql)
    sql = re.sub(r"(?i)\bAUTOINCREMENT\b", "", sql)
    sql = re.sub(r"(?i)\bREAL\b", "DOUBLE PRECISION", sql)
    sql = re.sub(r"(?i)\bINTEGER\b", "BIGINT", sql)
    sql = re.sub(r"(?i)\bBLOB\b", "BYTEA", sql)
    return sql


def translate_sql(sql: str) -> str:
    """Translate a SQLite-dialect statement into PostgreSQL dialect."""
    if not sql:
        return sql

    sql = sql.replace("datetime('now')", "now()")
    sql = sql.replace("datetime('now', 'localtime')", "now()")
    sql = sql.replace("datetime('now','localtime')", "now()")
    sql = sql.replace("julianday('now')", "EXTRACT(EPOCH FROM now())")

    # SQLite transaction-control keywords -> PostgreSQL.
    sql = re.sub(r"(?i)\bBEGIN\s+(IMMEDIATE|EXCLUSIVE)\b", "BEGIN", sql)

    # SQLite JSON + date functions -> PostgreSQL equivalents.
    sql = _JSON_EXTRACT_RE.sub(_json_extract_repl, sql)
    sql = _DATE_FN_RE.sub(lambda m: f"({m.group(1)})::date", sql)
    sql = _DATETIME_NOW_MOD_RE.sub(lambda m: f"now() + ({m.group(1)})::interval", sql)
    sql = _DATETIME_UNIX_RE.sub(lambda m: f"to_timestamp({m.group(1)})", sql)
    sql = _DATETIME_COL_RE.sub(lambda m: f"({m.group(1)})::timestamp", sql)

    # CREATE TABLE / ALTER DDL — map SQLite type names to PostgreSQL.
    if re.match(r"(?is)^\s*CREATE\s+TABLE", sql):
        sql = _translate_ddl_types(sql)

    # INSERT OR REPLACE -> ON CONFLICT (<first col>) DO UPDATE SET <rest>=EXCLUDED.<rest>
    m = _INSERT_OR_REPLACE_RE.search(sql)
    if m:
        table = m.group(1)
        cols = [c.strip() for c in m.group(2).split(",") if c.strip()]
        if cols:
            pk = cols[0]
            rest = cols[1:]
            sql = _INSERT_OR_REPLACE_RE.sub(
                f"INSERT INTO {table} ({', '.join(cols)})", sql, count=1
            )
            tail = sql.rstrip().rstrip(";")
            if rest:
                sets = ", ".join(f"{c}=EXCLUDED.{c}" for c in rest)
                sql = f"{tail} ON CONFLICT ({pk}) DO UPDATE SET {sets}"
            else:
                sql = f"{tail} ON CONFLICT ({pk}) DO NOTHING"

    # INSERT OR IGNORE -> ON CONFLICT DO NOTHING
    elif _INSERT_OR_IGNORE_RE.search(sql):
        sql = _INSERT_OR_IGNORE_RE.sub("INSERT INTO", sql, count=1)
        tail = sql.rstrip().rstrip(";")
        if not re.search(r"(?is)\bON\s+CONFLICT\b", tail):
            sql = f"{tail} ON CONFLICT DO NOTHING"

    # ALTER TABLE ADD COLUMN -> ADD COLUMN IF NOT EXISTS (idempotent migrations)
    sql = _ALTER_ADD_COLUMN_RE.sub(
        lambda mm: f"ALTER TABLE {mm.group(1)} ADD COLUMN IF NOT EXISTS {mm.group(2)}",
        sql,
    )

    sql = _replace_placeholders(sql)
    return sql


def _split_statements(script: str) -> list[str]:
    """Split a multi-statement script on top-level ``;`` (naive but sufficient
    for the schema scripts, which contain no semicolons inside string literals)."""
    parts = [p for p in script.split(";") if p.strip()]
    return parts


# ── Row + cursor wrappers ──────────────────────────────────────────────────

class DictRow:
    """A sqlite3.Row-compatible row: indexable by column name AND position."""

    __slots__ = ("_data",)

    def __init__(self, data: dict):
        self._data = data

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self._data.values())[key]
        return self._data[key]

    def keys(self):
        return list(self._data.keys())

    def values(self):
        return list(self._data.values())

    def get(self, key, default=None):
        return self._data.get(key, default)

    def __iter__(self):
        return iter(self._data.values())

    def __len__(self):
        return len(self._data)

    def __contains__(self, k):
        return k in self._data

    def __eq__(self, other):
        if isinstance(other, DictRow):
            return self._data == other._data
        return NotImplemented

    def __repr__(self):
        return repr(self._data)


class _NullCursor:
    """Dummy cursor returned for SQLite PRAGMA no-ops."""

    def __init__(self):
        self.rowcount = 0
        self.description = None
        self.lastrowid = None

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def fetchmany(self, size=None):
        return []

    def close(self):
        pass

    def __iter__(self):
        return iter([])


class PostgresCursor:
    def __init__(self, raw_cursor, lastrowid=None):
        self._raw = raw_cursor
        self._lastrowid = lastrowid
        self.rowcount = raw_cursor.rowcount if raw_cursor is not None else -1
        self.description = raw_cursor.description if raw_cursor is not None else None

    def fetchone(self):
        row = self._raw.fetchone() if self._raw else None
        return DictRow(row) if row else None

    def fetchall(self):
        rows = self._raw.fetchall() if self._raw else []
        return [DictRow(r) for r in rows]

    def fetchmany(self, size=None):
        rows = self._raw.fetchmany(size) if self._raw else []
        return [DictRow(r) for r in rows]

    @property
    def lastrowid(self):
        return self._lastrowid

    def close(self):
        if self._raw:
            try:
                self._raw.close()
            except Exception:
                pass

    def __iter__(self):
        for r in self.fetchall():
            yield r


# ── Error mapping (Postgres -> sqlite3.OperationalError) ───────────────────
# Existing code guards migrations with ``except sqlite3.OperationalError``.
# Map the "harmless" Postgres errors that those guards expect.

def _is_harmless_pg_error(exc: Exception) -> bool:
    if isinstance(exc, (
        pg_errors.DuplicateColumn, pg_errors.DuplicateTable, pg_errors.DuplicateObject,
        pg_errors.UndefinedTable, pg_errors.UndefinedColumn,
    )):
        return True
    msg = str(exc).lower()
    return any(k in msg for k in (
        "already exists", "does not exist", "no such table", "no such column",
    ))


class PostgresConnection:
    """sqlite3.Connection-compatible surface backed by psycopg2."""

    def __init__(self, dsn: str | None = None):
        self._dsn = dsn or default_dsn()
        self._conn: Any = None
        self.row_factory = None  # accepted for compatibility; rows are dicts already
        self.in_transaction = False
        self._lock = threading.Lock()
        self._reconnect()

    # -- connection lifecycle -------------------------------------------------
    def _reconnect(self):
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
        last = None
        for attempt in range(5):
            try:
                self._conn = psycopg2.connect(self._dsn, connect_timeout=20)
                self._conn.autocommit = False
                return
            except Exception as exc:  # noqa: BLE001
                last = exc
                time.sleep(0.5 * (2 ** attempt))
        raise RuntimeError(f"PostgreSQL connection failed: {last}")

    def _ensure_conn(self):
        if self._conn is None or self._conn.closed:
            self._reconnect()

    def execute(self, sql: str, params=None):
        sql = translate_sql(sql or "")
        self._ensure_conn()

        # PRAGMA statements are SQLite-specific maintenance no-ops on Postgres.
        if re.match(r"(?is)^\s*PRAGMA\b", sql):
            return _NullCursor()

        # SQLite BEGIN IMMEDIATE / BEGIN EXCLUSIVE is redundant on PostgreSQL:
        # psycopg2 (autocommit=False) already wraps every statement in an
        # implicit transaction until commit/rollback, and issuing a literal
        # BEGIN there raises "there is already a transaction in progress".
        # Close any dangling read transaction and no-op so callers that relied
        # on BEGIN IMMEDIATE for atomicity (workflow_queue.claim_next) still
        # get it — the next statement lazily starts a fresh transaction.
        if re.match(r"(?is)^\s*BEGIN\b", sql):
            try:
                if self._conn and not self._conn.closed:
                    self.rollback()
            except Exception:
                pass
            return _NullCursor()

        returning = None
        m = re.match(r"(?is)^\s*INSERT\s+INTO\s+\"?([A-Za-z_][A-Za-z0-9_]*)\"?", sql)
        if m and m.group(1).lower() in _SERIAL_ID_TABLES and not re.search(
            r"(?is)\bRETURNING\b", sql
        ):
            returning = m.group(1)
            sql = sql.rstrip().rstrip(";") + " RETURNING id"

        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        ctrl = self._conn.cursor()
        lastrowid = None
        try:
            # Savepoint so a failed statement (e.g. a harmless "no such table"
            # during migrations) aborts only itself, not the outer transaction —
            # mirroring sqlite3's per-statement error semantics. A separate
            # control cursor keeps the result cursor intact for SELECTs.
            ctrl.execute("SAVEPOINT sp_tp")
            try:
                cur.execute(sql, params if params is not None else ())
                if returning:
                    row = cur.fetchone()
                    if row:
                        lastrowid = row.get("id")
            except Exception as _stmt_exc:
                try:
                    ctrl.execute("ROLLBACK TO SAVEPOINT sp_tp")
                    ctrl.execute("RELEASE SAVEPOINT sp_tp")
                except Exception:
                    pass
                raise _stmt_exc
            else:
                ctrl.execute("RELEASE SAVEPOINT sp_tp")
        except Exception as exc:  # noqa: BLE001
            cur.close()
            ctrl.close()
            if _is_harmless_pg_error(exc):
                raise sqlite3.OperationalError(str(exc)) from exc
            raise
        ctrl.close()
        return PostgresCursor(cur, lastrowid=lastrowid)

    def executescript(self, script: str):
        for stmt in _split_statements(script):
            self.execute(stmt)
        self.commit()

    def commit(self):
        if self._conn:
            self._conn.commit()
            self.in_transaction = False

    def rollback(self):
        if self._conn:
            try:
                self._conn.rollback()
            except Exception:
                pass
            self.in_transaction = False

    def close(self):
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            try:
                self.commit()
            except Exception:
                self.rollback()
        else:
            self.rollback()
        return False

    def __getattr__(self, name):
        # Delegate any remaining attribute to the underlying psycopg2 connection.
        return getattr(self._conn, name)


# ── DSN resolution ─────────────────────────────────────────────────────────

def default_dsn() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if url:
        return url
    # Local development defaults to localhost (no Docker network alias).
    # docker-compose sets PGHOST=postgres explicitly for the container so the
    # app reaches the postgres service on the compose network.
    host = os.getenv("PGHOST", "localhost")
    port = os.getenv("PGPORT", "5432")
    db = os.getenv("PGDATABASE", "technopoliss")
    user = os.getenv("PGUSER", "technopoliss")
    password = os.getenv("PGPASSWORD", "technopoliss")
    return f"host={host} port={port} dbname={db} user={user} password={password}"


class PostgresConnectionPool:
    """Thread-local pool mirroring the old per-thread SelfHealingConnectionProxy."""

    def __init__(self):
        self._local = threading.local()

    def get(self) -> PostgresConnection:
        if not getattr(self._local, "conn", None):
            self._local.conn = PostgresConnection()
        else:
            conn = self._local.conn
            try:
                conn._ensure_conn()
                if conn.in_transaction:
                    conn.rollback()
            except Exception:
                conn._reconnect()
        return self._local.conn

    def new(self) -> PostgresConnection:
        return PostgresConnection()

    def close(self):
        conn = getattr(self._local, "conn", None)
        if conn:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None
