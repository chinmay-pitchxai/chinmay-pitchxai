"""SQLite-based persistent storage — replaces fragile JSON files."""

from __future__ import annotations

import json
import sqlite3
import threading
import asyncio
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Any
from zoneinfo import ZoneInfo
from loguru import logger

# Lazy import to avoid circular deps at module level
_INVALIDATED = False
def _invalidate_state_cache():
    global _INVALIDATED
    try:
        from core.kv_cache import invalidate_all as _do_invalidate
        _do_invalidate()
    except ImportError:
        pass
    conn = None
    try:
        conn = _get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO app_meta(key, value) VALUES (?, ?)",
            ("last_cache_invalidation_time", str(time.time())),
        )
        conn.commit()
    except Exception as e:
        logger.error(f"Failed to save cache invalidation timestamp: {e}")
        if conn:
            try:
                conn.rollback()
            except Exception as re:
                logger.error(f"Failed rollback in invalidate_state_cache: {re}")

_DB_PATH: Optional[Path] = None
_LOCAL = threading.local()

# ── PostgreSQL connection pool (replaces SQLite) ────────────────────────────
_PG_POOL = None


def _pg_pool():
    global _PG_POOL
    if _PG_POOL is None:
        from core.db import PostgresConnectionPool

        _PG_POOL = PostgresConnectionPool()
    return _PG_POOL

# ── In-memory paused-sources store (shared across all threads in process) ──
# Avoids cross-thread SQLite WAL snapshot issues where a fresh thread-local
# connection might not immediately see a just-committed write from another thread.
_PAUSED_SOURCES: dict[str, list[str]] = {}  # role -> list[filename]
_PAUSED_SOURCES_LOCK = threading.Lock()

# Inter-call gap (seconds) between outbound dials.
_GAP_LEGACY_DEFAULT_SEC = 120.0
_GAP_CORE_ROLE_NAMES = frozenset({"sales_1"})
STRICT_CORE_GAP_MIN_SEC = 120.0
STRICT_CORE_GAP_MAX_SEC = 180.0
STRICT_CORE_GAP_SEC = 150.0
_GAP_CORE_PRODUCT_ROLES_SEC = STRICT_CORE_GAP_SEC


def is_strict_gap_core_role(role: Optional[str]) -> bool:
    return (role or "sales_1").strip().lower() in _GAP_CORE_ROLE_NAMES


def default_inter_call_gap_sec(role: Optional[str]) -> float:
    r = (role or "sales_1").strip().lower()
    if r in _GAP_CORE_ROLE_NAMES:
        return float(_GAP_CORE_PRODUCT_ROLES_SEC)
    return float(_GAP_LEGACY_DEFAULT_SEC)


def init_db(data_dir: Optional[Path | str] = None) -> Path:
    """Initialize the SQLite database. Call once at startup."""
    global _DB_PATH
    if isinstance(data_dir, str):
        data_dir = Path(data_dir)
    base = data_dir or Path(__file__).resolve().parent.parent / "data"
    base.mkdir(parents=True, exist_ok=True)
    _DB_PATH = base / "vernika.db"
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS role_state (
            role TEXT PRIMARY KEY,
            prompt TEXT DEFAULT '',
            rag TEXT DEFAULT '',
            delay_sec REAL DEFAULT 5.0,
            vobiz_config TEXT DEFAULT '{}',
            updated_at TEXT DEFAULT (datetime('now')),
            greeting_text TEXT DEFAULT ''
        );
    """)
    # Migration: add greeting_text if missing
    try:
        conn.execute("ALTER TABLE role_state ADD COLUMN greeting_text TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass # Already exists

    # Migration: add campaign_config for Outpero-style campaign settings
    try:
        conn.execute("ALTER TABLE role_state ADD COLUMN campaign_config TEXT DEFAULT '{}'")
    except sqlite3.OperationalError:
        pass # Already exists

    # Migration: add P1-P9 phone number columns for sandbox configuration
    for i in range(1, 10):
        try:
            conn.execute(f"ALTER TABLE role_state ADD COLUMN p{i}_number TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass # Already exists

    # Campaign contacts table (individual contacts added via form/paste)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS campaign_contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            phone TEXT NOT NULL,
            name TEXT DEFAULT '',
            extra TEXT DEFAULT '{}',
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_campaign_contacts_role ON campaign_contacts(role)")
    conn.commit()

    # Per-role campaign Cases. The operator defines one or more named "Cases"
    # (e.g. "April Steel Sheets Push", "Diwali Discount Drive") and **activates
    # exactly one** per role. The bridge appends the active case description
    # to the system prompt so the AI runs today's campaign without editing the
    # base persona prompt.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS cases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_cases_role ON cases(role);
        CREATE INDEX IF NOT EXISTS idx_cases_active ON cases(role, active);
    """)
    conn.commit()

    # Per-role campaign schedules. The operator uploads leads, then schedules
    # the campaign to start automatically at a future date/time. A small
    # background loop in ``core.worker`` polls this table every 30 s and, when
    # ``run_at <= now`` and ``status='scheduled'``, kicks off the same worker
    # the Start Campaign button does. ``run_at`` is stored as epoch seconds
    # (UTC) so timezone math is trivial both server- and client-side.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            name TEXT DEFAULT '',
            run_at REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'scheduled',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            started_at REAL,
            error TEXT,
            stop_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_schedules_role ON schedules(role);
        CREATE INDEX IF NOT EXISTS idx_schedules_due ON schedules(status, run_at);
    """)
    conn.commit()
    # Migration: installs created before stop_at existed need the column added
    # *before* the index that references it can be created. Split the work so
    # CREATE INDEX never runs against a missing column.
    try:
        conn.execute("ALTER TABLE schedules ADD COLUMN stop_at REAL")
    except sqlite3.OperationalError:
        pass  # Already exists
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_schedules_stop ON schedules(status, stop_at)"
    )
    conn.commit()

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS manual_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            camp_id TEXT NOT NULL UNIQUE,
            to_phone TEXT NOT NULL DEFAULT '',
            callee_name TEXT NOT NULL DEFAULT '',
            log_id TEXT,
            status TEXT NOT NULL DEFAULT 'dialing',
            started_at TEXT DEFAULT (datetime('now')),
            ended_at TEXT,
            updated_at TEXT DEFAULT (datetime('now')),
            duration_sec REAL,
            disposition TEXT DEFAULT '',
            summary TEXT DEFAULT '',
            next_steps TEXT DEFAULT '',
            emotion_label TEXT DEFAULT '',
            emotion_rationale TEXT DEFAULT '',
            emotion_confidence REAL,
            analysis_json TEXT DEFAULT '{}',
            error TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_manual_calls_role_started
            ON manual_calls(role, id DESC);
    """)
    conn.commit()

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS incoming_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            camp_id TEXT NOT NULL UNIQUE,
            from_phone TEXT NOT NULL DEFAULT '',
            caller_name TEXT NOT NULL DEFAULT '',
            log_id TEXT,
            status TEXT NOT NULL DEFAULT 'ringing',
            started_at TEXT DEFAULT (datetime('now')),
            ended_at TEXT,
            updated_at TEXT DEFAULT (datetime('now')),
            duration_sec REAL,
            disposition TEXT DEFAULT '',
            summary TEXT DEFAULT '',
            next_steps TEXT DEFAULT '',
            emotion_label TEXT DEFAULT '',
            emotion_rationale TEXT DEFAULT '',
            emotion_confidence REAL,
            analysis_json TEXT DEFAULT '{}',
            error TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_incoming_calls_role_started
            ON incoming_calls(role, id DESC);
    """)
    conn.commit()
    for _col_sql in (
        "ALTER TABLE incoming_calls ADD COLUMN to_phone TEXT DEFAULT ''",
        "ALTER TABLE incoming_calls ADD COLUMN callback_scheduled INTEGER NOT NULL DEFAULT 0",
    ):
        try:
            conn.execute(_col_sql)
        except sqlite3.OperationalError:
            pass
    conn.commit()

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            name TEXT DEFAULT 'Unknown',
            phone TEXT NOT NULL,
            email TEXT DEFAULT '',
            company TEXT DEFAULT '',
            details TEXT DEFAULT '',
            extra TEXT DEFAULT '{}',
            status TEXT DEFAULT 'pending',
            analysis TEXT DEFAULT '{}',
            start_time REAL,
            error TEXT,
            _log_id TEXT,
            _call_id TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS agents (
            id TEXT PRIMARY KEY,
            role TEXT NOT NULL DEFAULT 'factory',
            name TEXT NOT NULL,
            prompt TEXT NOT NULL,
            voice TEXT DEFAULT 'Puck',
            knowledge_files TEXT DEFAULT '[]',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS prompt_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            prompt TEXT NOT NULL,
            rag TEXT DEFAULT '',
            greeting_text TEXT DEFAULT '',
            version_number INTEGER NOT NULL,
            status TEXT DEFAULT 'active',
            created_at TEXT DEFAULT (datetime('now')),
            created_by TEXT DEFAULT 'admin',
            notes TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS agent_leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT NOT NULL,
            lead_id TEXT NOT NULL,
            name TEXT DEFAULT 'Unknown',
            phone TEXT NOT NULL,
            email TEXT DEFAULT '',
            company TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_leads_role ON leads(role);
        CREATE INDEX IF NOT EXISTS idx_leads_phone ON leads(phone);
        CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(role, status);
        CREATE INDEX IF NOT EXISTS idx_agent_leads_agent ON agent_leads(agent_id);
        CREATE INDEX IF NOT EXISTS idx_leads_role_created ON leads(role, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_leads_role_start_time ON leads(role, start_time DESC);
    """)
    conn.commit()

    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_leads_role_status_created ON leads(role, status, created_at DESC);
    """)
    conn.commit()

    # ``extra``: JSON blob for CSV columns beyond name/phone/email/company.
    # IMPORTANT: ALTER must run *after* ``CREATE TABLE IF NOT EXISTS leads`` so new
    # installs get the column and older DBs (created before ``extra``) are migrated.
    try:
        conn.execute("ALTER TABLE leads ADD COLUMN extra TEXT DEFAULT '{}'")
    except sqlite3.OperationalError:
        pass  # Already exists

    # Migration: whatsapp_sent flag to prevent duplicate WhatsApp sends
    try:
        conn.execute("ALTER TABLE leads ADD COLUMN whatsapp_sent INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    # Migration: failed_call_retries to track retry attempts for unanswered calls
    try:
        conn.execute("ALTER TABLE leads ADD COLUMN failed_call_retries INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    # Migration: whatsapp_sent_at and whatsapp_reminder_sent for 24h follow-up messages
    try:
        conn.execute("ALTER TABLE leads ADD COLUMN whatsapp_sent_at REAL")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE leads ADD COLUMN whatsapp_reminder_sent INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    # Migration: email_sent and email_sent_at for email deduplication
    try:
        conn.execute("ALTER TABLE leads ADD COLUMN email_sent INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE leads ADD COLUMN email_sent_at REAL")
    except sqlite3.OperationalError:
        pass

    # Migration: first_called_at REAL to keep the anchor of the first outbound campaign attempt
    try:
        conn.execute("ALTER TABLE leads ADD COLUMN first_called_at REAL")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("UPDATE leads SET first_called_at = start_time WHERE first_called_at IS NULL AND start_time IS NOT NULL")
        conn.commit()
    except Exception:
        pass

    # Migration: outbound_phone TEXT to track which phone number made the call
    try:
        conn.execute("ALTER TABLE leads ADD COLUMN outbound_phone TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    # Migration: source TEXT to distinguish cold vs digital leads for sandbox routing
    try:
        conn.execute("ALTER TABLE leads ADD COLUMN source TEXT DEFAULT 'campaign'")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    # Migration: add sandbox column for campaign sandbox routing (1-4)
    try:
        conn.execute("ALTER TABLE leads ADD COLUMN sandbox INTEGER DEFAULT 1")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    # Migration: add role column to agents
    try:
        conn.execute("ALTER TABLE agents ADD COLUMN role TEXT NOT NULL DEFAULT 'factory'")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    # Seed default roles if empty
    for role in (
        "sales_1",
    ):
        conn.execute(
            "INSERT OR IGNORE INTO role_state (role) VALUES (?);",
            (role,)
        )
    conn.commit()

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS app_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT ''
        );
    """)
    conn.commit()

    # Per-role scheduled callbacks. Agents can schedule individual callbacks
    # at a specific future time. The campaign worker picks these up at the
    # scheduled moment and calls them immediately (bypassing the normal
    # inter-call gap) or queues them if the role is currently on a call.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scheduled_callbacks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            phone TEXT NOT NULL,
            name TEXT DEFAULT '',
            lead_id INTEGER,
            scheduled_at REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'scheduled',
            error TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sc_role ON scheduled_callbacks(role)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sc_due ON scheduled_callbacks(role, status, scheduled_at)")
    conn.commit()

    # Migration: analysis columns for scheduled_callbacks (outcome tracking on dashboard)
    for _col_sql in (
        "ALTER TABLE scheduled_callbacks ADD COLUMN disposition TEXT DEFAULT ''",
        "ALTER TABLE scheduled_callbacks ADD COLUMN summary TEXT DEFAULT ''",
        "ALTER TABLE scheduled_callbacks ADD COLUMN rating REAL",
        "ALTER TABLE scheduled_callbacks ADD COLUMN next_action TEXT DEFAULT '{}'",
        "ALTER TABLE scheduled_callbacks ADD COLUMN analysis_json TEXT DEFAULT '{}'",
        "ALTER TABLE scheduled_callbacks ADD COLUMN outbound_phone TEXT DEFAULT ''",
        "ALTER TABLE scheduled_callbacks ADD COLUMN user_review TEXT DEFAULT ''",
        "ALTER TABLE scheduled_callbacks ADD COLUMN callback_type TEXT DEFAULT ''",
        "ALTER TABLE scheduled_callbacks ADD COLUMN follow_up_number INTEGER",
    ):
        try:
            conn.execute(_col_sql)
        except sqlite3.OperationalError:
            pass

    for _col_sql in (
        "ALTER TABLE call_attempts ADD COLUMN call_category TEXT DEFAULT 'initial'",
        "ALTER TABLE call_attempts ADD COLUMN follow_up_number INTEGER",
    ):
        try:
            conn.execute(_col_sql)
        except sqlite3.OperationalError:
            pass

    # Virtual meet tracking (no calendar automation — pure tracking/display)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS virtual_meets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            meet_date TEXT NOT NULL,
            meet_time TEXT NOT NULL,
            notes TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'scheduled',
            rescheduled_from_id INTEGER,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (lead_id) REFERENCES leads(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_vm_lead ON virtual_meets(lead_id);
        CREATE INDEX IF NOT EXISTS idx_vm_role ON virtual_meets(role);
    """)
    conn.commit()

    # WhatsApp conversation history for AI-based auto-replies
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS conversation_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('user','assistant')),
            message TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_conv_phone ON conversation_messages(phone);
        CREATE INDEX IF NOT EXISTS idx_conv_phone_created ON conversation_messages(phone, created_at);
    """)
    conn.commit()

    # Per-call attempt history — every call (including retakes) logs an entry
    # so the dashboard can show timing, recording, summary, and transcript per attempt.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS call_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            attempt_number INTEGER NOT NULL DEFAULT 1,
            log_id TEXT,
            status TEXT NOT NULL DEFAULT 'completed',
            disposition TEXT DEFAULT '',
            summary TEXT DEFAULT '',
            rating REAL,
            duration_sec REAL,
            callback_scheduled_at REAL,
            error TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ca_lead ON call_attempts(lead_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ca_role ON call_attempts(role)")
    conn.commit()

    # Outbound camp sessions — lets Vobiz webhook host hydrate call context when
    # the dialer runs on a different machine (local dashboard + VPS callbacks).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS camp_sessions (
            camp_id TEXT PRIMARY KEY,
            role TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            connected_at REAL,
            ended_at REAL,
            log_id TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_camp_sessions_role ON camp_sessions(role)")
    conn.commit()

    # Vobiz CallUUID → camp/log mapping (survives process restart for recording ingest)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS vobiz_call_map (
            call_uuid TEXT PRIMARY KEY,
            camp_id TEXT NOT NULL DEFAULT '',
            log_id TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL DEFAULT '',
            phone TEXT NOT NULL DEFAULT '',
            auth_id TEXT NOT NULL DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_vobiz_call_map_log ON vobiz_call_map(log_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_vobiz_call_map_camp ON vobiz_call_map(camp_id)"
    )
    conn.commit()

    # ── Autonomous orchestration schema (4-sandbox / P1-P9 state machine) ──
    # ``leads`` gains lifecycle columns; the workflow job queue, per-lead memory,
    # site visits, feedback records, and the TRAI DND register are all created
    # here so the orchestration service works on a fresh install.
    for _col_sql in (
        "ALTER TABLE leads ADD COLUMN lifecycle_status TEXT DEFAULT 'new'",
        "ALTER TABLE leads ADD COLUMN orchestration_version INTEGER DEFAULT 0",
        "ALTER TABLE leads ADD COLUMN segment TEXT DEFAULT ''",
        "ALTER TABLE leads ADD COLUMN source_file TEXT DEFAULT ''",
    ):
        try:
            conn.execute(_col_sql)
        except sqlite3.OperationalError:
            pass  # Already exists

    for _col_sql in (
        "ALTER TABLE call_attempts ADD COLUMN retry_cycle TEXT DEFAULT ''",
        "ALTER TABLE call_attempts ADD COLUMN from_number TEXT DEFAULT ''",
        "ALTER TABLE call_attempts ADD COLUMN outcome TEXT DEFAULT ''",
        "ALTER TABLE call_attempts ADD COLUMN started_at REAL",
        "ALTER TABLE call_attempts ADD COLUMN ended_at REAL",
        "ALTER TABLE call_attempts ADD COLUMN job_id INTEGER",
        "ALTER TABLE call_attempts ADD COLUMN call_id TEXT DEFAULT ''",
    ):
        try:
            conn.execute(_col_sql)
        except sqlite3.OperationalError:
            pass
    conn.commit()

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS workflow_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL,
            job_type TEXT NOT NULL,
            source_type TEXT NOT NULL DEFAULT '',
            source_id TEXT NOT NULL DEFAULT '',
            priority INTEGER NOT NULL DEFAULT 5,
            status TEXT NOT NULL DEFAULT 'scheduled',
            due_at_utc REAL NOT NULL,
            eligible_pool TEXT NOT NULL DEFAULT '',
            attempt_number INTEGER NOT NULL DEFAULT 0 CHECK(attempt_number BETWEEN 0 AND 3),
            claimed_by_number TEXT,
            claim_token TEXT,
            claimed_at REAL,
            lease_expires_at REAL,
            idempotency_key TEXT NOT NULL UNIQUE,
            payload_json TEXT NOT NULL DEFAULT '{}',
            error TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_wj_status_due ON workflow_jobs(status, due_at_utc);
        CREATE INDEX IF NOT EXISTS idx_wj_lead ON workflow_jobs(lead_id);
        CREATE INDEX IF NOT EXISTS idx_wj_pool_status ON workflow_jobs(eligible_pool, status);

        CREATE TABLE IF NOT EXISTS lead_memory (
            lead_id INTEGER PRIMARY KEY,
            facts_json TEXT NOT NULL DEFAULT '{}',
            summary TEXT DEFAULT '',
            last_interaction_at REAL,
            version INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS site_visits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL,
            scheduled_at_utc REAL NOT NULL,
            family_members TEXT DEFAULT '',
            preferred_unit TEXT DEFAULT '',
            budget TEXT DEFAULT '',
            location TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'scheduled',
            version INTEGER NOT NULL DEFAULT 1,
            completed_at REAL,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_sv_lead ON site_visits(lead_id);

        CREATE TABLE IF NOT EXISTS feedback_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL,
            site_visit_id INTEGER,
            job_id INTEGER,
            outcome TEXT DEFAULT '',
            details_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_fr_visit ON feedback_records(site_visit_id);

        CREATE TABLE IF NOT EXISTS do_not_contact (
            normalized_phone TEXT PRIMARY KEY,
            lead_id INTEGER,
            reason TEXT DEFAULT '',
            source_interaction TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS whatsapp_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL,
            phone TEXT NOT NULL DEFAULT '',
            message_type TEXT NOT NULL DEFAULT 'brochure',
            direction TEXT NOT NULL DEFAULT 'outbound',
            content TEXT DEFAULT '',
            sent_at REAL,
            replied_at REAL,
            status TEXT NOT NULL DEFAULT 'sent',
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_wa_lead ON whatsapp_messages(lead_id);
        CREATE INDEX IF NOT EXISTS idx_wa_type ON whatsapp_messages(lead_id, message_type);
    """)
    conn.commit()

    # Create role-specific data directories for prompt + RAG files
    for role in ("sales_1",):
        role_dir = base / role
        role_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"✅ Created directory: {role_dir}")
    
    # Initialize Vobiz accounts (if configured in .env)
    from config import settings
    role_vobiz_map = {
        "sales_1": {
            "auth_id": settings.vobiz_sales_1_auth_id or settings.vobiz_auth_id,
            "auth_token": settings.vobiz_sales_1_auth_token or settings.vobiz_auth_token,
            "from_number": settings.vobiz_sales_1_phone_1 or settings.vobiz_from_number,
            "public_url": settings.vobiz_public_base_url,
            "phone_numbers": [
                settings.vobiz_sales_1_phone_1 or "",
                settings.vobiz_sales_1_phone_2 or "",
            ],
        },
    }
    
    for role, vobiz_creds in role_vobiz_map.items():
        cur = conn.execute("SELECT vobiz_config FROM role_state WHERE role = ?", (role,))
        row = cur.fetchone()
        db_config_str = row["vobiz_config"] if row else "{}"
        try:
            db_config = json.loads(db_config_str) if db_config_str else {}
        except Exception:
            db_config = {}
        is_empty_db = not db_config or db_config == {}

        # Check if role-specific Vobiz environment credentials are set explicitly
        has_explicit_env = bool(
            settings.vobiz_sales_1_auth_id and settings.vobiz_sales_1_auth_token
        )

        # Local dev (127.0.0.1 / localhost SERVER_URL): always refresh from .env so stale VPS URLs
        # in role_state cannot hijack Vobiz webhooks after switching from production.
        server_url = (settings.server_url or "").lower()
        is_local_dev = "127.0.0.1" in server_url or "localhost" in server_url
        env_public = (settings.vobiz_public_base_url or "").strip().rstrip("/")
        db_public = str(db_config.get("public_url") or "").strip().rstrip("/")
        stale_public = bool(env_public and db_public and env_public != db_public)

        if is_empty_db or has_explicit_env or is_local_dev or stale_public:
            if vobiz_creds["auth_id"] and vobiz_creds["auth_token"] and vobiz_creds["from_number"]:
                vobiz_config = {
                    "auth_id": vobiz_creds["auth_id"],
                    "auth_token": vobiz_creds["auth_token"],
                    "from_number": vobiz_creds["from_number"],
                    "public_url": settings.vobiz_public_base_url,
                }
                phone_numbers = vobiz_creds.get("phone_numbers") or []
                phone_numbers = [n for n in phone_numbers if n]
                if phone_numbers:
                    vobiz_config["phone_numbers"] = phone_numbers
                conn.execute(
                    "UPDATE role_state SET vobiz_config = ?, updated_at = datetime('now') WHERE role = ?",
                    (json.dumps(vobiz_config), role)
                )
                conn.commit()
                logger.info(f"✅ Initialized role '{role}' in database")
        else:
            logger.info(f"ℹ️  Preserving existing database configuration for role '{role}'")

    # ── Startup migration: do NOT blanket-reset failed leads (3-attempt policy) ──
    # Leads that exhausted retries stay failed; others keep their retry schedule.
    try:
        max_retries = max(0, int(__import__("config").settings.failed_call_max_attempts) - 1)
        requeued = conn.execute(
            """
            UPDATE leads
            SET status = 'pending',
                error  = NULL,
                updated_at = datetime('now')
            WHERE status IN ('failed', 'error')
              AND COALESCE(CAST(json_extract(extra, '$.failed_call_retries') AS INTEGER), 0) < ?
            """,
            (max_retries,),
        ).rowcount
        conn.commit()
        if requeued:
            logger.info(
                "Startup migration: re-queued {} failed/error leads with retries remaining → pending",
                requeued,
            )
    except Exception as _mig_err:
        logger.warning("Startup migration (re-queue failed leads) skipped: {}", _mig_err)

    close_db()
    logger.info("Database initialized (PostgreSQL): {}", _DB_PATH)
    return _DB_PATH



import functools

class SelfHealingConnectionProxy:
    def __init__(self, db_path):
        self._db_path = db_path
        self._conn = None
        self._reconnect()

    def _reconnect(self):
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
        retries = 5
        for i in range(retries):
            try:
                self._conn = sqlite3.connect(
                    str(self._db_path),
                    check_same_thread=False,
                    timeout=30.0,
                )
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")
                self._conn.execute("PRAGMA wal_autocheckpoint=100")
                self._conn.execute("PRAGMA busy_timeout=10000")
                self._conn.row_factory = sqlite3.Row
                self._conn.execute("PRAGMA foreign_keys = ON")
                return
            except Exception as e:
                logger.error(f"Failed to connect to SQLite (attempt {i+1}/{retries}): {e}")
                if i == retries - 1:
                    raise
                time.sleep(0.5)

    def close(self):
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def __enter__(self):
        return self._conn.__enter__()

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            return self._conn.__exit__(exc_type, exc_val, exc_tb)
        except (sqlite3.OperationalError, sqlite3.DatabaseError) as e:
            logger.error(f"Database error during transaction commit/rollback exit: {e}")
            raise

    def __getattr__(self, name):
        attr = getattr(self._conn, name)
        if callable(attr):
            @functools.wraps(attr)
            def wrapped(*args, **kwargs):
                nonlocal attr
                retries = 5
                last_err = None
                for i in range(retries):
                    try:
                        return attr(*args, **kwargs)
                    except (sqlite3.OperationalError, sqlite3.DatabaseError) as e:
                        last_err = e
                        msg = str(e).lower()
                        if "locked" in msg or "busy" in msg or "malformed" in msg or "corrupt" in msg:
                            logger.warning(f"Database error during {name} ({e}), self-healing reconnect attempt {i+1}/{retries}...")
                            self._reconnect()
                            attr = getattr(self._conn, name)
                            time.sleep(0.2 * (2 ** i))
                            continue
                        raise
                logger.error(f"Database operation {name} failed after self-healing retries: {last_err}")
                raise last_err
            return wrapped
        return attr

def _get_conn():
    """Thread-local PostgreSQL connection (sqlite3-API-compatible shim)."""
    if _DB_PATH is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _pg_pool().get()


def new_db_connection():
    """Return an independent PostgreSQL connection for a long-running async worker."""
    if _DB_PATH is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _pg_pool().new()


def close_db() -> None:
    _pg_pool().close()


def cleanup_orphaned_operational_rows() -> dict[str, int]:
    """Remove queue and memory rows whose parent lead no longer exists."""
    conn = _get_conn()
    removed: dict[str, int] = {}
    for table in ("workflow_jobs", "lead_memory", "site_visits", "feedback_records", "call_attempts"):
        try:
            cur = conn.execute(
                f'DELETE FROM "{table}" WHERE lead_id NOT IN (SELECT id FROM leads)'
            )
            removed[table] = max(0, int(cur.rowcount or 0))
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc).lower():
                raise
    conn.commit()
    return removed


def _dashboard_tz() -> ZoneInfo:
    try:
        from config import settings

        return ZoneInfo((settings.transcript_callback_tz or "Asia/Kolkata").strip() or "Asia/Kolkata")
    except Exception:
        return ZoneInfo("Asia/Kolkata")


# Operator clicked Start → survives process restart until Stop or graceful empty queue.
_CAMPAIGN_WANT_META_PREFIX = "campaign_want_running_v2"


def campaign_want_running_meta_key(role: str) -> str:
    return f"{_CAMPAIGN_WANT_META_PREFIX}:{(role or 'sales_1').strip().lower()}"


async def set_campaign_want_running(role: str, wanted: bool) -> None:
    return await asyncio.to_thread(_set_campaign_want_running_sync, role, wanted)

def _set_campaign_want_running_sync(role: str, wanted: bool) -> None:
    conn = _get_conn()
    k = campaign_want_running_meta_key(role)
    if wanted:
        conn.execute(
            "INSERT OR REPLACE INTO app_meta(key, value) VALUES (?, ?)",
            (k, "1"),
        )
    else:
        conn.execute("DELETE FROM app_meta WHERE key = ?", (k,))
    conn.commit()


async def roles_with_campaign_run_wanted() -> list[str]:
    return await asyncio.to_thread(_roles_with_campaign_run_wanted_sync)

def _roles_with_campaign_run_wanted_sync() -> list[str]:
    conn = _get_conn()
    prefix = f"{_CAMPAIGN_WANT_META_PREFIX}:"
    rows = conn.execute(
        """
        SELECT key FROM app_meta
        WHERE key LIKE ?
          AND trim(value) IN ('1', 'true', 'yes')
        """,
        (prefix + "%",),
    ).fetchall()
    out: list[str] = []
    for r in rows:
        key = str(r["key"] or "")
        if key.startswith(prefix):
            out.append(key[len(prefix):])
    return out


# Operator clicked Stop / stop-all — blocks auto-resume on deploy restart until Start.
_CAMPAIGN_PAUSED_META = "campaign_globally_paused_v1"


async def set_campaign_globally_paused(paused: bool) -> None:
    return await asyncio.to_thread(_set_campaign_globally_paused_sync, paused)


def _set_campaign_globally_paused_sync(paused: bool) -> None:
    conn = _get_conn()
    if paused:
        conn.execute(
            "INSERT OR REPLACE INTO app_meta(key, value) VALUES (?, ?)",
            (_CAMPAIGN_PAUSED_META, "1"),
        )
    else:
        conn.execute("DELETE FROM app_meta WHERE key = ?", (_CAMPAIGN_PAUSED_META,))
    conn.commit()


async def is_campaign_globally_paused() -> bool:
    return await asyncio.to_thread(_is_campaign_globally_paused_sync)


def _is_campaign_globally_paused_sync() -> bool:
    conn = _get_conn()
    row = conn.execute(
        "SELECT value FROM app_meta WHERE key = ?",
        (_CAMPAIGN_PAUSED_META,),
    ).fetchone()
    return bool(row and str(row["value"] or "").strip().lower() in ("1", "true", "yes"))


# --- Role State ---

async def get_role_state(role: str) -> dict:
    return await asyncio.to_thread(_get_role_state_sync, role)

def _get_role_state_sync(role: str) -> dict:
    role_key = (role or "sales_1").strip().lower()
    fallback_delay = default_inter_call_gap_sec(role_key)
    conn = _get_conn()
    row = conn.execute("SELECT * FROM role_state WHERE role = ?", (role_key,)).fetchone()
    if not row:
        return {
            "role": role_key,
            "prompt": "",
            "rag": "",
            "delay_sec": fallback_delay,
            "vobiz": {},
        }
    ds = row["delay_sec"]
    result = {
        "role": row["role"],
        "prompt": row["prompt"] or "",
        "rag": row["rag"] or "",
        "delay_sec": float(fallback_delay if ds is None else ds),
        "vobiz": json.loads(row["vobiz_config"] or "{}"),
        "greeting_text": row["greeting_text"] or "",
    }
    # Include P1-P9 phone numbers if columns exist
    for i in range(1, 10):
        key = f"p{i}_number"
        try:
            result[key] = row[key] or ""
        except (IndexError, KeyError):
            result[key] = ""
    return result


async def save_role_state(role: str, prompt: str = None, rag: str = None, vobiz_config: dict = None, delay_sec: float = None, greeting_text: str = None, **phone_numbers):
    return await asyncio.to_thread(_save_role_state_sync, role, prompt, rag, vobiz_config, delay_sec, greeting_text, **phone_numbers)

def _save_role_state_sync(role: str, prompt: str = None, rag: str = None, vobiz_config: dict = None, delay_sec: float = None, greeting_text: str = None, **phone_numbers):
    conn = _get_conn()
    role = (role or "sales_1").strip().lower()
    # Ensure a row exists — bare UPDATE silently affects 0 rows if the role was never inserted.
    conn.execute("INSERT OR IGNORE INTO role_state (role) VALUES (?)", (role,))
    updates = []
    params = []
    if prompt is not None:
        updates.append("prompt = ?")
        params.append(prompt)
    if rag is not None:
        updates.append("rag = ?")
        params.append(rag)
    if vobiz_config is not None:
        updates.append("vobiz_config = ?")
        params.append(json.dumps(vobiz_config))
    if delay_sec is not None:
        updates.append("delay_sec = ?")
        params.append(delay_sec)
    if greeting_text is not None:
        updates.append("greeting_text = ?")
        params.append(greeting_text)
    # Save P1-P9 phone numbers
    for i in range(1, 10):
        key = f"p{i}_number"
        if key in phone_numbers and phone_numbers[key] is not None:
            updates.append(f"{key} = ?")
            params.append(phone_numbers[key])

    if not updates:
        return

    updates.append("updated_at = datetime('now')")
    params.append(role)
    conn.execute(f"UPDATE role_state SET {', '.join(updates)} WHERE role = ?", params)
    conn.commit()
    _invalidate_state_cache()


# --- Leads ---

async def get_lead(role: str, lead_id: int) -> Optional[dict]:
    return await asyncio.to_thread(_get_lead_sync, role, lead_id)

def _get_lead_sync(role: str, lead_id: int) -> Optional[dict]:
    """Single campaign lead row keyed by SQLite ``id`` and ``role``."""
    conn = _get_conn()
    r = (role or "sales_1").strip().lower()
    row = conn.execute(
        "SELECT * FROM leads WHERE role = ? AND id = ?",
        (r, int(lead_id)),
    ).fetchone()
    return _row_to_dict(row) if row else None


def resolve_lead_session_log_id_sync(
    role: str,
    lead_id: int | None,
    phone: str,
    *,
    current_log_id: str = "",
) -> str:
    """Best log_id for transcript/recording lookup (row, sibling duplicate, or camp_sessions)."""
    log_id = str(current_log_id or "").strip()
    if log_id:
        return log_id
    conn = _get_conn()
    r = (role or "sales_1").strip().lower()
    if lead_id is not None:
        row = conn.execute(
            "SELECT _log_id FROM leads WHERE role = ? AND id = ?",
            (r, int(lead_id)),
        ).fetchone()
        if row and row[0]:
            return str(row[0]).strip()
    digits = "".join(c for c in str(phone or "") if c.isdigit())[-10:]
    if digits:
        sibling = conn.execute(
            """
            SELECT _log_id FROM leads
            WHERE role = ? AND phone LIKE ? AND COALESCE(TRIM(_log_id), '') != ''
            ORDER BY start_time DESC, updated_at DESC
            LIMIT 1
            """,
            (r, f"%{digits}"),
        ).fetchone()
        if sibling and sibling[0]:
            return str(sibling[0]).strip()
    if lead_id is not None:
        camp = conn.execute(
            """
            SELECT log_id FROM call_attempts
            WHERE lead_id = ? AND COALESCE(TRIM(log_id), '') != ''
            ORDER BY id DESC LIMIT 1
            """,
            (int(lead_id),),
        ).fetchone()
        if camp and camp[0]:
            return str(camp[0]).strip()
    return ""


def sync_lead_log_ids_from_attempts_sync() -> int:
    """Backfill leads._log_id from latest call_attempts row (recordings/transcripts lookup)."""
    conn = _get_conn()
    cur = conn.execute(
        """
        UPDATE leads
        SET _log_id = (
            SELECT ca.log_id FROM call_attempts ca
            WHERE ca.lead_id = leads.id AND COALESCE(TRIM(ca.log_id), '') != ''
            ORDER BY ca.id DESC LIMIT 1
        ),
        updated_at = datetime('now')
        WHERE COALESCE(TRIM(_log_id), '') = ''
          AND EXISTS (
            SELECT 1 FROM call_attempts ca2
            WHERE ca2.lead_id = leads.id AND COALESCE(TRIM(ca2.log_id), '') != ''
          )
        """
    )
    conn.commit()
    n = int(cur.rowcount or 0)
    if n:
        _invalidate_state_cache()
    return n


async def get_leads(
    role: str,
    status: str = None,
    limit: int = 1000,
    *,
    order: str = "created",
    modulo: int = None,
    remainder: int = None,
) -> list[dict]:
    return await asyncio.to_thread(_get_leads_sync, role, status, limit, order, modulo, remainder)

def _get_leads_sync(
    role: str,
    status: str = None,
    limit: int = 1000,
    order: str = "created",
    modulo: int = None,
    remainder: int = None,
) -> list[dict]:
    conn = _get_conn()
    query = "SELECT * FROM leads WHERE role = ?"
    params = [role]
    if status:
        query += " AND status = ?"
        params.append(status)
    if modulo is not None and remainder is not None:
        query += " AND id % ? = ?"
        params.extend([modulo, remainder])
    if (order or "created").strip().lower() == "activity":
        query += """
         ORDER BY
             CASE WHEN start_time IS NOT NULL AND CAST(start_time AS REAL) > 0
                  THEN CAST(start_time AS REAL) ELSE 0.0 END DESC,
             updated_at DESC,
             created_at DESC
         LIMIT ?
        """
    else:
        query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    return [_row_to_dict(r) for r in rows]


def _outbound_called_sql_where() -> str:
    """Shared predicate for called-count + called-cohort list (matches frontend ``isCalled``)."""
    return """
          (
                (COALESCE(TRIM(COALESCE(_log_id, '')), '') != '')
             OR (start_time IS NOT NULL AND CAST(start_time AS REAL) > 0)
             OR (LOWER(COALESCE(status, '')) IN (
                    'failed', 'error', 'no answer', 'no-answer', 'busy',
                    'completed', 'not_interested', 'interested',
                    'callback_scheduled', 'callback_completed',
                    'site_visit', 'site_visited'
                ))
          )
    """


async def count_leads_with_outbound_attempt(role: str) -> int:
    return await asyncio.to_thread(_count_leads_with_outbound_attempt_sync, role)

def _count_leads_with_outbound_attempt_sync(role: str) -> int:
    """How many rows have evidence of at least one dial / bridge session started.

    Mirrors the dashboard ``isCalled`` heuristic without loading every row."""
    conn = _get_conn()
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS c FROM leads WHERE role = ?
          AND {_outbound_called_sql_where()}
        """,
        ((role or "sales_1").strip().lower(),),
    ).fetchone()
    return int(row["c"]) if row else 0


async def count_call_attempts(role: str, since_date: str | None = None) -> int:
    return await asyncio.to_thread(count_call_attempts_sync, role, since_date)


def count_call_attempts_sync(role: str, since_date: str | None = None) -> int:
    """Total dial attempts for a role (includes retries to the same number).

    ``since_date`` is optional ISO date ``YYYY-MM-DD`` (inclusive, UTC date of created_at).
    """
    conn = _get_conn()
    role = (role or "sales_1").strip().lower()
    if since_date:
        row = conn.execute(
            """
            SELECT COUNT(*) AS c FROM call_attempts
            WHERE role = ? AND date(created_at) >= date(?)
            """,
            (role, since_date),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM call_attempts WHERE role = ?",
            (role,),
        ).fetchone()
    return int(row["c"]) if row else 0


def call_attempts_timeline_sync(
    role: str,
    dates: list,
    *,
    tz_name: str = "Asia/Kolkata",
) -> list[int]:
    """Per-day attempt counts aligned with ``dates`` (dashboard calendar days in ``tz_name``).

    ``dates`` is a list of ``datetime.date`` objects. Counts use ``created_at`` converted
    to the dashboard timezone when possible; falls back to UTC date(created_at).
    """
    from datetime import datetime, timezone
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name or "Asia/Kolkata")
    except Exception:
        tz = timezone.utc

    role = (role or "sales_1").strip().lower()
    if not dates:
        return []
    start = min(dates).isoformat()
    end = max(dates).isoformat()
    conn = _get_conn()
    rows = conn.execute(
        """
        SELECT created_at FROM call_attempts
        WHERE role = ?
          AND date(created_at) >= date(?)
          AND date(created_at) <= date(?)
        """,
        (role, start, end),
    ).fetchall()
    idx = {d: i for i, d in enumerate(dates)}
    out = [0] * len(dates)
    for row in rows:
        raw = str(row["created_at"] or "").strip()
        if not raw:
            continue
        day = None
        try:
            txt = raw.replace("Z", "+00:00")
            if "T" not in txt and " " in txt:
                txt = txt.replace(" ", "T", 1)
            if "+" not in txt[-6:] and not txt.endswith("Z") and "T" in txt:
                # SQLite datetime('now') is UTC-ish naive — treat as UTC
                dt = datetime.fromisoformat(txt).replace(tzinfo=timezone.utc)
            else:
                dt = datetime.fromisoformat(txt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            day = dt.astimezone(tz).date()
        except (ValueError, TypeError, OSError):
            try:
                day = datetime.strptime(raw[:10], "%Y-%m-%d").date()
            except (ValueError, TypeError):
                continue
        if day in idx:
            out[idx[day]] += 1
    return out


def call_attempt_exists_for_log_id_sync(log_id: str) -> bool:
    lid = (log_id or "").strip()
    if not lid:
        return False
    conn = _get_conn()
    row = conn.execute(
        "SELECT 1 FROM call_attempts WHERE log_id = ? LIMIT 1",
        (lid[:200],),
    ).fetchone()
    return bool(row)


def find_lead_id_for_log_id_sync(role: str, log_id: str) -> int | None:
    """Resolve lead_id for a session log_id via leads._log_id or call_attempts."""
    lid = (log_id or "").strip()
    if not lid:
        return None
    role = (role or "").strip().lower()
    conn = _get_conn()
    if role:
        row = conn.execute(
            "SELECT id FROM leads WHERE role = ? AND _log_id = ? LIMIT 1",
            (role, lid[:200]),
        ).fetchone()
        if row:
            return int(row["id"])
        row = conn.execute(
            "SELECT lead_id FROM call_attempts WHERE role = ? AND log_id = ? LIMIT 1",
            (role, lid[:200]),
        ).fetchone()
        if row:
            return int(row["lead_id"])
    row = conn.execute(
        "SELECT id, role FROM leads WHERE _log_id = ? LIMIT 1",
        (lid[:200],),
    ).fetchone()
    if row:
        return int(row["id"])
    row = conn.execute(
        "SELECT lead_id FROM call_attempts WHERE log_id = ? LIMIT 1",
        (lid[:200],),
    ).fetchone()
    if row:
        return int(row["lead_id"])
    return None


async def get_leads_with_outbound_activity(role: str, limit: int = 32000) -> list[dict]:
    return await asyncio.to_thread(_get_leads_with_outbound_activity_sync, role, limit)

def _get_leads_with_outbound_activity_sync(role: str, limit: int = 32000) -> list[dict]:
    """All campaign leads that have been bridged/outbound-dialed.

    Engagement timeline aggregates use this rather than the small ``chart_sample`` slice so
    activity on older CSV rows still appears alongside ``called_count``.
    """

    role = (role or "sales_1").strip().lower()
    lim = max(1, min(int(limit), 50000))
    conn = _get_conn()
    rows = conn.execute(
        f"""
        SELECT * FROM leads WHERE role = ?
          AND {_outbound_called_sql_where()}
        ORDER BY
             CASE WHEN start_time IS NOT NULL AND CAST(start_time AS REAL) > 0
                  THEN CAST(start_time AS REAL) ELSE 0.0 END DESC,
             updated_at DESC
        LIMIT ?
        """,
        (role, lim),
    ).fetchall()
    return [_row_to_dict(row) for row in rows]


async def add_lead(role: str, name: str, phone: str, email: str = "", company: str = "", details: str = "") -> int:
    return await asyncio.to_thread(_add_lead_sync, role, name, phone, email, company, details)

def _add_lead_sync(role: str, name: str, phone: str, email: str = "", company: str = "", details: str = "") -> int:
    from core.dnc import is_phone_blocked
    if is_phone_blocked(phone):
        logger.warning(f"Blocked lead add: {phone} is in DNC list")
        return -1
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO leads (role, name, phone, email, company, details) VALUES (?, ?, ?, ?, ?, ?)",
        (role, name, phone, email, company, details)
    )
    conn.commit()
    _invalidate_state_cache()
    return cur.lastrowid


async def bulk_add_leads(role: str, leads: list[dict]) -> int:
    count, _skipped, _dnc = await asyncio.to_thread(_bulk_add_leads_sync, role, leads)
    return count

def _bulk_add_leads_sync(role: str, leads: list[dict]) -> tuple[int, int, int]:
    """Insert leads. Returns (saved_count, duplicate_skips, dnc_blocked)."""
    """Insert leads, persisting any **extra** caller fields into ``extra`` JSON.

    Deduplicates by normalized phone per role — skips duplicates in DB and within the same upload batch.
    """
    from core.phone_norm import norm_phone_str
    from core.state import normalize_console_role

    role = normalize_console_role(role)
    conn = _get_conn()
    count = 0
    skipped = 0
    dnc_blocked = 0
    existing: set[str] = set()
    for row in conn.execute("SELECT phone FROM leads WHERE role = ?", (role,)).fetchall():
        norm = norm_phone_str(str(row[0] or ""))
        if norm:
            existing.add(norm)
    _RESERVED = {
        "name", "phone", "email", "company", "details",
        "status", "role", "id", "extra", "source", "sandbox",
    }
    for lead in leads:
        phone = norm_phone_str(str(lead.get("phone", "") or "").strip())
        if not phone:
            continue
        if phone in existing:
            skipped += 1
            continue
        from core.dnc import is_phone_blocked
        if is_phone_blocked(phone):
            logger.warning(f"Skipping bulk add for DNC blocked number: {phone}")
            dnc_blocked += 1
            continue
        raw_extra = lead.get("extra")
        if isinstance(raw_extra, dict):
            extras_dict = {k: v for k, v in raw_extra.items() if v not in (None, "")}
        else:
            extras_dict = {
                k: v for k, v in lead.items()
                if k not in _RESERVED and v not in (None, "")
            }
        extras_dict = {str(k): str(v) for k, v in extras_dict.items() if str(v).strip()}
        # Persist upload provenance (original file name / Google Sheet broker) into
        # ``extra`` so the dashboard Source filter can group by upload source even
        # when the caller supplied a full ``extra`` dict (e.g. digital Excel ingests).
        for _prov_key in ("upload_source", "source_file"):
            prov_val = lead.get(_prov_key)
            if prov_val and not extras_dict.get(_prov_key):
                extras_dict[_prov_key] = str(prov_val)
        extra_json = json.dumps(extras_dict, ensure_ascii=False) if extras_dict else "{}"
        lead_source = lead.get("source", "campaign") or "campaign"
        lead_sandbox = max(1, min(4, int(lead.get("sandbox", 1) or 1)))
        conn.execute(
            "INSERT INTO leads (role, name, phone, email, company, details, extra, status, source, sandbox) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                role,
                lead.get("name", "Unknown"),
                phone,
                lead.get("email", ""),
                lead.get("company", ""),
                lead.get("details", ""),
                extra_json,
                "pending",
                lead_source,
                lead_sandbox,
            )
        )
        existing.add(phone)
        count += 1
    conn.commit()
    _invalidate_state_cache()
    if skipped:
        logger.info(f"Bulk add dedup: skipped {skipped} duplicate phone(s) for role={role}")
    if dnc_blocked:
        logger.info(f"Bulk add DNC: blocked {dnc_blocked} phone(s) for role={role}")
    return count, skipped, dnc_blocked


def _get_lead_role_sync(lead_id: int) -> Optional[str]:
    conn = _get_conn()
    row = conn.execute("SELECT role FROM leads WHERE id = ?", (lead_id,)).fetchone()
    return str(row["role"]) if row else None

async def get_lead_role(lead_id: int) -> Optional[str]:
    return await asyncio.to_thread(_get_lead_role_sync, lead_id)

async def update_lead_status(lead_id: int, status: str, error: str = None, analysis: dict = None):
    res = await asyncio.to_thread(_update_lead_status_sync, lead_id, status, error, analysis)
    try:
        from core.events import get_event_bus
        role = await get_lead_role(lead_id)
        if role:
            await get_event_bus().publish("lead_updated", role=role, lead_id=lead_id)
    except Exception:
        pass
    return res


async def update_lead_sandbox(lead_id: int, new_sandbox: int) -> bool:
    """Update a lead's sandbox assignment when it transitions between sandboxes.

    Sandbox transitions per the implementation plan:
      SB1 → SB2: On failed call (retry engine)
      SB1/SB2 → SB3: On interested/site visit (nurture)
      SB3 → SB4: On site-visit completed (feedback)

    This function is called by the worker after each call outcome to
    ensure the lead's sandbox column reflects its current position in
    the pipeline.
    """
    return await asyncio.to_thread(_update_lead_sandbox_sync, lead_id, new_sandbox)


def _update_lead_sandbox_sync(lead_id: int, new_sandbox: int) -> bool:
    conn = _get_conn()
    sandbox = max(1, min(4, int(new_sandbox or 1)))
    cur = conn.execute(
        "UPDATE leads SET sandbox = ?, updated_at = datetime('now') WHERE id = ?",
        (sandbox, lead_id),
    )
    conn.commit()
    return cur.rowcount > 0


async def update_lead_info(lead_id: int, *, name: str = None, email: str = None) -> bool:
    return await asyncio.to_thread(_update_lead_info_sync, lead_id, name, email)


def _update_lead_info_sync(lead_id: int, name: str = None, email: str = None) -> bool:
    conn = _get_conn()
    updates = []
    params = []
    if name is not None:
        updates.append("name = ?")
        params.append(name)
    if email is not None:
        updates.append("email = ?")
        params.append(email)
    if not updates:
        return False
    updates.append("updated_at = datetime('now')")
    params.append(lead_id)
    cur = conn.execute(f"UPDATE leads SET {', '.join(updates)} WHERE id = ?", params)
    conn.commit()
    _invalidate_state_cache()
    return cur.rowcount > 0


def _update_lead_status_sync(lead_id: int, status: str, error: str = None, analysis: dict = None):
    conn = _get_conn()
    s_lower = (status or "").lower()

    # Capture old status for DashboardState notification
    _old_status = "pending"
    try:
        _row = conn.execute("SELECT status FROM leads WHERE id = ?", (lead_id,)).fetchone()
        if _row:
            _old_status = str(_row["status"] or "pending").strip().lower()
    except Exception:
        pass

    # Map status -> lifecycle_status so both columns stay in sync
    _status_to_lifecycle = {
        "pending": "new",
        "dialing": "campaign_calling",
        "completed": "connected",
        "failed": "failed_retry_waiting",
        "not_interested": "not_interested",
        "interested": "interested",
        "callback_scheduled": "callback_requested",
        "site_visit": "site_visit_scheduled",
    }
    _lifecycle = _status_to_lifecycle.get(s_lower, s_lower)

    # When a lead is released back to 'pending' or starts 'dialing',
    # clear its call markers so it is NOT counted as "called" and doesn't
    # carry stale analysis/ratings from previous attempts.
    clear_on_pending = (s_lower in ("pending", "dialing"))
    if analysis:
        if clear_on_pending:
            conn.execute(
                "UPDATE leads SET status = ?, lifecycle_status = ?, error = NULL, analysis = NULL, "
                "start_time = NULL, _log_id = NULL, _call_id = NULL, updated_at = datetime('now') WHERE id = ?",
                (status, _lifecycle, lead_id),
            )
        else:
            conn.execute(
                "UPDATE leads SET status = ?, lifecycle_status = ?, error = ?, analysis = ?, updated_at = datetime('now') WHERE id = ?",
                (status, _lifecycle, error, json.dumps(analysis), lead_id)
            )
    else:
        if clear_on_pending:
            conn.execute(
                "UPDATE leads SET status = ?, lifecycle_status = ?, error = NULL, analysis = NULL, start_time = NULL, "
                "_log_id = NULL, _call_id = NULL, updated_at = datetime('now') WHERE id = ?",
                (status, _lifecycle, lead_id),
            )
        else:
            conn.execute(
                "UPDATE leads SET status = ?, lifecycle_status = ?, error = ?, updated_at = datetime('now') WHERE id = ?",
                (status, _lifecycle, error, lead_id)
            )
    
    # Cancel pending scheduled retries if resolved
    if s_lower in ("completed", "not_interested", "callback_completed", "callback_scheduled", "site_visit", "site_visited", "interested", "dnc"):
        conn.execute(
            "DELETE FROM scheduled_callbacks WHERE lead_id = ? AND status = 'scheduled'",
            (lead_id,)
        )
        
    conn.commit()
    _invalidate_state_cache()

    # Notify materialized dashboard state
    try:
        from core.dashboard_state import notify_lead_updated
        notify_lead_updated(
            role=str(conn.execute("SELECT role FROM leads WHERE id = ?", (lead_id,)).fetchone()["role"]),
            lead_id=lead_id, old_status=_old_status, new_status=s_lower,
            analysis_raw=analysis,
        )
    except Exception:
        pass


async def update_lead_call_info(lead_id: int, log_id: str = None, call_id: str = None, start_time: float = None, outbound_phone: str = None):
    res = await asyncio.to_thread(_update_lead_call_info_sync, lead_id, log_id, call_id, start_time, outbound_phone)
    try:
        from core.events import get_event_bus
        role = await get_lead_role(lead_id)
        if role:
            await get_event_bus().publish("lead_updated", role=role, lead_id=lead_id)
    except Exception:
        pass
    return res

def _update_lead_call_info_sync(lead_id: int, log_id: str = None, call_id: str = None, start_time: float = None, outbound_phone: str = None):
    conn = _get_conn()
    updates = []
    params = []
    if log_id is not None:
        updates.append("_log_id = ?")
        params.append(log_id)
    if call_id is not None:
        updates.append("_call_id = ?")
        params.append(call_id)
    if start_time is not None:
        updates.append("start_time = ?")
        params.append(start_time)
        updates.append("first_called_at = COALESCE(first_called_at, ?)")
        params.append(start_time)
    if outbound_phone is not None:
        updates.append("outbound_phone = ?")
        params.append(outbound_phone)
    updates.append("updated_at = datetime('now')")
    params.append(lead_id)
    conn.execute(f"UPDATE leads SET {', '.join(updates)} WHERE id = ?", params)
    conn.commit()
    _invalidate_state_cache()


async def reschedule_leads(
    role: str,
    from_date_iso: str,
    to_date_iso: str,
    outcomes: list[str],
    target_epoch: float,
) -> int:
    return await asyncio.to_thread(
        _reschedule_leads_sync, role, from_date_iso, to_date_iso, outcomes, target_epoch
    )


def _reschedule_leads_sync(
    role: str,
    from_date_iso: str,
    to_date_iso: str,
    outcomes: list[str],
    target_epoch: float,
) -> int:
    """Reschedule historical campaign leads for a future callback.

    ``from_date_iso`` and ``to_date_iso`` are inclusive dates (YYYY-MM-DD).
    ``outcomes`` maps from frontend-friendly keys to DB filters:
      - failed_no_answer  -> status in ('failed', 'error', 'no answer')
      - interested        -> disposition == 'Interested'
      - cut_in_middle     -> status in ('failed', 'completed') with a start_time
      - not_interested    -> status == 'not_interested' or disposition == 'Not Interested'
    """
    from datetime import datetime, time, timezone

    role = (role or "sales_1").strip().lower()
    tz = timezone.utc
    try:
        from_dt = datetime.strptime(from_date_iso, "%Y-%m-%d").replace(tzinfo=tz)
        to_dt = datetime.strptime(to_date_iso, "%Y-%m-%d").replace(tzinfo=tz)
    except ValueError:
        raise ValueError("Dates must be YYYY-MM-DD")

    from_epoch = from_dt.timestamp()
    to_epoch = datetime.combine(to_dt.date(), time.max, tzinfo=tz).timestamp()

    if not outcomes:
        return 0

    outcome_conditions = []
    for oc in outcomes:
        if oc == "failed_no_answer":
            outcome_conditions.append("status IN ('failed', 'error', 'no answer')")
        elif oc == "interested":
            outcome_conditions.append(
                "(json_extract(analysis, '$.disposition') = 'Interested' OR status = 'completed')"
            )
        elif oc == "cut_in_middle":
            outcome_conditions.append(
                "(status IN ('failed', 'completed') AND start_time IS NOT NULL AND CAST(start_time AS REAL) > 0)"
            )
        elif oc == "not_interested":
            outcome_conditions.append(
                "(status = 'not_interested' OR json_extract(analysis, '$.disposition') = 'Not Interested')"
            )

    if not outcome_conditions:
        return 0

    where_sql = "role = ? AND start_time IS NOT NULL AND CAST(start_time AS REAL) >= ? AND CAST(start_time AS REAL) <= ? AND (" + " OR ".join(outcome_conditions) + ")"

    conn = _get_conn()
    # Load matching rows so we can merge callback_reminder_epoch into their analysis JSON.
    rows = conn.execute(
        f"SELECT id, analysis FROM leads WHERE {where_sql}",
        (role, from_epoch, to_epoch),
    ).fetchall()

    updated = 0
    for row in rows:
        try:
            analysis = json.loads(row["analysis"] or "{}") if row["analysis"] else {}
        except Exception:
            analysis = {}
        analysis["callback_reminder_epoch"] = float(target_epoch)
        analysis["rescheduled_at_epoch"] = time.time()
        analysis["rescheduled_from_status"] = "campaign_reschedule"
        conn.execute(
            "UPDATE leads SET status = 'callback_scheduled', analysis = ?, updated_at = datetime('now') WHERE id = ?",
            (json.dumps(analysis), row["id"]),
        )
        updated += 1

    conn.commit()
    _invalidate_state_cache()
    logger.info(
        "Rescheduled {} lead(s) for role={} between {} and {} to callback epoch {}",
        updated, role, from_date_iso, to_date_iso, target_epoch
    )
    return updated


async def retry_all_failed_leads(role: str) -> int:
    return await asyncio.to_thread(_retry_all_failed_leads_sync, role)

def _retry_all_failed_leads_sync(role: str) -> int:
    role = (role or "sales_1").strip().lower()
    conn = _get_conn()
    
    rows = conn.execute(
        """
        SELECT id, extra, analysis FROM leads 
        WHERE role = ? 
          AND (
               status IN ('failed', 'error', 'busy', 'no answer', 'no response', 'no_response')
            OR json_extract(analysis, '$.disposition') IN ('Failed', 'No Answer', 'Busy', 'Wrong Number', 'Not Available', 'Voicemail', 'No Response')
          )
        """,
        (role,)
    ).fetchall()
    
    updated = 0
    for row in rows:
        lead_id = row["id"]
        try:
            extra = json.loads(row["extra"] or "{}") if row["extra"] else {}
        except Exception:
            extra = {}
        try:
            analysis = json.loads(row["analysis"] or "{}") if row["analysis"] else {}
        except Exception:
            analysis = {}
            
        extra["failed_call_retries"] = 0
        
        # Reset the callback reminders if any
        if "callback_reminder_epoch" in analysis:
            del analysis["callback_reminder_epoch"]
        if "requested_callback_datetime_iso" in analysis:
            del analysis["requested_callback_datetime_iso"]
            
        # Delete all pending/scheduled callbacks for this lead to avoid duplicates when redialing
        conn.execute(
            "DELETE FROM scheduled_callbacks WHERE lead_id = ? AND status IN ('pending', 'scheduled', 'queued')",
            (lead_id,)
        )
        
        conn.execute(
            """
            UPDATE leads 
            SET status = 'pending', 
                start_time = NULL, 
                error = NULL, 
                _log_id = NULL, 
                _call_id = NULL, 
                extra = ?, 
                analysis = '{}', 
                updated_at = datetime('now') 
            WHERE id = ?
            """,
            (json.dumps(extra), lead_id)
        )
        updated += 1
        
    conn.commit()
    _invalidate_state_cache()
    logger.info("Reset {} failed lead(s) to pending for role={}", updated, role)
    return updated


async def promote_due_scheduled_callbacks(now_epoch: float | None = None) -> int:
    return await asyncio.to_thread(_promote_due_scheduled_callbacks_sync, now_epoch)

def _promote_due_scheduled_callbacks_sync(now_epoch: float | None = None) -> int:
    """Move leads whose defer-until epoch has passed.

    Promotes:
      - ``callback_scheduled`` → ``pending``
      - ``busy`` / ``failed`` / ``no answer`` → ``pending`` when callback_reminder_epoch is due.
    """

    t = float(now_epoch if now_epoch is not None else time.time())
    conn = None
    try:
        conn = _get_conn()
        # 1. Classic callback_scheduled → pending
        cur = conn.execute(
            """
            UPDATE leads SET status = 'pending',
                   updated_at = datetime('now')
             WHERE status = 'callback_scheduled'
               AND json_extract(analysis, '$.callback_reminder_epoch') IS NOT NULL
               AND CAST(json_extract(analysis, '$.callback_reminder_epoch') AS REAL) > 0
               AND CAST(json_extract(analysis, '$.callback_reminder_epoch') AS REAL) <= ?
               AND id NOT IN (
                 SELECT lead_id FROM scheduled_callbacks
                 WHERE lead_id IS NOT NULL
                   AND status IN ('scheduled','queued','calling')
               )
            """,
            (t,),
        )
        n1 = int(cur.rowcount or 0)

        # 2. Busy / failed / no-answer / no-response leads whose retry cooldown expired
        cur2 = conn.execute(
            """
            UPDATE leads SET status = 'pending',
                   updated_at = datetime('now')
             WHERE status IN ('busy', 'failed', 'no answer', 'no response', 'no_response')
               AND json_extract(analysis, '$.callback_reminder_epoch') IS NOT NULL
               AND CAST(json_extract(analysis, '$.callback_reminder_epoch') AS REAL) > 0
               AND CAST(json_extract(analysis, '$.callback_reminder_epoch') AS REAL) <= ?
               AND id NOT IN (
                 SELECT lead_id FROM scheduled_callbacks
                 WHERE lead_id IS NOT NULL
                   AND status IN ('scheduled','queued','calling')
               )
            """,
            (t,),
        )
        n2 = int(cur2.rowcount or 0)

        conn.commit()
        n = n1 + n2
        if n > 0:
            logger.info(f"Promoted {n1} callback_scheduled + {n2} busy/failed/no-answer → pending (due recall)")
            _invalidate_state_cache()
        return n
    except Exception as e:
        logger.error(f"Failed to promote due scheduled callbacks: {e}")
        if conn:
            try:
                conn.rollback()
            except Exception as re:
                logger.error(f"Failed rollback in promote_due_scheduled_callbacks: {re}")
        return 0


async def role_has_future_callback_scheduled(role: str, now_epoch: float) -> bool:
    return await asyncio.to_thread(_role_has_future_callback_scheduled_sync, role, now_epoch)

def _role_has_future_callback_scheduled_sync(role: str, now_epoch: float) -> bool:
    """True if ``role`` has at least one lead waiting for a future transcript-requested recall."""

    from core.state import normalize_console_role as _norm

    rid = _norm(role)
    conn = _get_conn()
    row = conn.execute(
        """
        SELECT 1 FROM leads
        WHERE role = ?
          AND status = 'callback_scheduled'
          AND json_extract(analysis, '$.callback_reminder_epoch') IS NOT NULL
          AND CAST(json_extract(analysis, '$.callback_reminder_epoch') AS REAL) > ?
        LIMIT 1
        """,
        (rid, float(now_epoch)),
    ).fetchone()
    return row is not None


async def role_has_pending_scheduled_callbacks(role: str) -> bool:
    """True if ``role`` has any pending/scheduled items in the scheduled_callbacks table.

    Keeps the campaign sub-worker alive even when the pending leads queue is empty,
    so overdue callbacks (failed-call retries, user-requested recalls) are always
    executed instead of being orphaned when the queue drains.
    """
    return await asyncio.to_thread(_role_has_pending_scheduled_callbacks_sync, role)

def _role_has_pending_scheduled_callbacks_sync(role: str) -> bool:
    from core.state import normalize_console_role as _norm
    rid = _norm(role)
    conn = _get_conn()
    row = conn.execute(
        """
        SELECT 1 FROM scheduled_callbacks
        WHERE role = ?
          AND status IN ('scheduled', 'queued', 'pending')
        LIMIT 1
        """,
        (rid,),
    ).fetchone()
    return row is not None


async def role_has_due_scheduled_callbacks(role: str, now_epoch: float) -> bool:
    """True if ``role`` has at least one callback due now (scheduled_at <= now)."""
    return await asyncio.to_thread(_role_has_due_scheduled_callbacks_sync, role, now_epoch)


def _role_has_due_scheduled_callbacks_sync(role: str, now_epoch: float) -> bool:
    from core.state import normalize_console_role as _norm
    rid = _norm(role)
    conn = _get_conn()
    row = conn.execute(
        """
        SELECT 1 FROM scheduled_callbacks
        WHERE role = ?
          AND status IN ('scheduled', 'queued')
          AND scheduled_at <= ?
        LIMIT 1
        """,
        (rid, float(now_epoch)),
    ).fetchone()
    return row is not None


async def role_has_due_scheduled_callbacks_for_phone(
    role: str, outbound_phone: str, now_epoch: float
) -> bool:
    """True if a callback is due for this specific outbound line."""
    return await asyncio.to_thread(
        _role_has_due_scheduled_callbacks_for_phone_sync, role, outbound_phone, now_epoch
    )


def _role_has_due_scheduled_callbacks_for_phone_sync(
    role: str, outbound_phone: str, now_epoch: float
) -> bool:
    from core.phone_norm import norm_phone_str
    from core.state import normalize_console_role as _norm

    rid = _norm(role)
    norm_out = norm_phone_str(outbound_phone)
    conn = _get_conn()
    rows = conn.execute(
        """
        SELECT outbound_phone FROM scheduled_callbacks
        WHERE role = ?
          AND status IN ('scheduled', 'queued')
          AND scheduled_at <= ?
        """,
        (rid, float(now_epoch)),
    ).fetchall()
    for row in rows:
        cb_out = norm_phone_str(str(row["outbound_phone"] or ""))
        if cb_out and cb_out == norm_out:
            return True
    return False


async def delete_campaign_source(role: str, source_name: str) -> int:
    """Delete all leads belonging to an upload source."""
    return await asyncio.to_thread(_delete_campaign_source_sync, role, source_name)


def _delete_campaign_source_sync(role: str, source_name: str) -> int:
    from core.state import normalize_console_role as _norm
    rid = _norm(role)
    src = (source_name or "").strip()
    if not src:
        return 0
    conn = _get_conn()
    cur = conn.execute(
        """
        DELETE FROM leads
        WHERE role = ?
          AND json_extract(extra, '$.upload_source') = ?
        """,
        (rid, src),
    )
    deleted = cur.rowcount or 0
    conn.commit()
    _invalidate_state_cache()
    return deleted


async def reset_leads(role: str):
    return await asyncio.to_thread(_reset_leads_sync, role)

def _reset_leads_sync(role: str):
    conn = _get_conn()
    conn.execute("UPDATE leads SET status = 'pending', lifecycle_status = 'new', error = NULL, extra = NULL, analysis = NULL, start_time = NULL, updated_at = datetime('now') WHERE role = ?", (role,))
    conn.commit()
    _invalidate_state_cache()


async def wipe_leads(role: str):
    return await asyncio.to_thread(_wipe_leads_sync, role)

def _wipe_leads_sync(role: str):
    from core.state import normalize_console_role
    role = normalize_console_role(role)
    conn = _get_conn()
    conn.execute("DELETE FROM leads WHERE role = ?", (role,))
    conn.commit()
    set_paused_sources_sync(role, [])
    _invalidate_state_cache()
    try:
        from core.dashboard_state import invalidate_role as _dash_invalidate_role
        _dash_invalidate_role(role)
    except Exception as exc:
        logger.warning("DashboardState invalidate after wipe failed for role={}: {}", role, exc)


# ── Full role-scoped wipe (mirrors scripts/wipe_local_full.py, one role) ──

# Tables that carry a console ``role`` column → delete only this role's rows.
_ROLE_SCOPED_WIPE_TABLES = (
    "leads",
    "call_attempts",
    "scheduled_callbacks",
    "vobiz_call_map",
    "manual_calls",
    "camp_sessions",
    "campaign_contacts",
    "incoming_calls",
    "virtual_meets",
    "schedules",
    "cases",
)

# Tables without a console ``role`` column → delete all rows (mirrors
# wipe_local_full.py). ``conversation_messages.role`` is the chat author
# role ('user'/'assistant'), not a console role, so it is not scoped here.
_FULL_WIPE_TABLES = (
    "workflow_jobs",
    "whatsapp_messages",
    "feedback_records",
    "site_visits",
    "lead_memory",
    "conversation_messages",
    "agent_leads",
    "do_not_contact",
    "dnc_list",
)

# (table, log_id column) pairs whose transcript/recording files should be
# removed alongside the DB rows for this role.
_LOG_ID_SOURCES = (
    ("leads", "_log_id"),
    ("call_attempts", "log_id"),
    ("manual_calls", "log_id"),
    ("incoming_calls", "log_id"),
    ("scheduled_callbacks", "log_id"),
    ("camp_sessions", "log_id"),
)


def _collect_role_log_ids_sync(conn, role: str) -> list[str]:
    log_ids: set[str] = set()
    for table, col in _LOG_ID_SOURCES:
        try:
            rows = conn.execute(
                f'SELECT "{col}" FROM "{table}" WHERE role = ? AND "{col}" IS NOT NULL AND "{col}" != ?',
                (role, ""),
            ).fetchall()
        except sqlite3.OperationalError:
            continue
        for row in rows:
            val = str(row[col] or "").strip()
            if val:
                log_ids.add(val)
    return sorted(log_ids)


async def wipe_all_role_data(role: str) -> list[str]:
    """Full role-scoped wipe: leads + all campaign/operational tables + paused
    meta + cache invalidation (mirrors ``scripts/wipe_local_full.py`` for one
    role). Returns the collected ``log_id`` values so the caller can remove the
    role's transcript/recording files."""
    return await asyncio.to_thread(_wipe_all_role_data_sync, role)


def _wipe_all_role_data_sync(role: str) -> list[str]:
    from core.state import normalize_console_role
    role = normalize_console_role(role)
    conn = _get_conn()
    log_ids = _collect_role_log_ids_sync(conn, role)

    for table in _ROLE_SCOPED_WIPE_TABLES:
        conn.execute(f'DELETE FROM "{table}" WHERE role = ?', (role,))
    for table in _FULL_WIPE_TABLES:
        try:
            conn.execute(f'DELETE FROM "{table}"')
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "no such table" not in msg and "does not exist" not in msg:
                raise
    try:
        conn.execute("DELETE FROM campaign_states")
    except sqlite3.OperationalError as exc:
        msg = str(exc).lower()
        if "no such table" not in msg and "does not exist" not in msg:
            raise

    # Role-scoped paused / want-running meta (current + legacy key variants).
    paused_key = f"paused_sources:{role}"
    want_key = campaign_want_running_meta_key(role)
    conn.execute(
        "DELETE FROM app_meta WHERE key IN (?, ?, ?, ?)",
        (
            paused_key,
            want_key,
            f"campaign_want_running:{role}",
            f"campaign_want_running_v1:{role}",
        ),
    )
    with _PAUSED_SOURCES_LOCK:
        _PAUSED_SOURCES.pop(role, None)
    conn.commit()
    _invalidate_state_cache()
    try:
        from core.kv_cache import invalidate_role as _do_invalidate_role
        _do_invalidate_role(role)
    except Exception as exc:
        logger.warning("kv_cache invalidate after wipe failed for role={}: {}", role, exc)
    try:
        from core.dashboard_state import invalidate_role as _dash_invalidate_role
        _dash_invalidate_role(role)
    except Exception as exc:
        logger.warning("DashboardState invalidate after wipe failed for role={}: {}", role, exc)
    return log_ids


async def get_lead_counts(role: str) -> dict:
    return await asyncio.to_thread(_get_lead_counts_sync, role)

def _get_lead_counts_sync(role: str) -> dict:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT status, COUNT(*) as count FROM leads WHERE role = ? GROUP BY status",
        (role,)
    ).fetchall()
    counts = {"total": 0, "pending": 0, "dialing": 0, "completed": 0, "failed": 0, "not_interested": 0, "interested": 0, "callback_scheduled": 0, "site_visit": 0, "busy": 0, "no_answer": 0, "error": 0, "dnc": 0}
    for row in rows:
        status = row["status"]
        count = row["count"]
        counts[status] = count if status in counts else count
        counts["total"] += count
    # WhatsApp & Email sent counts
    wa_row = conn.execute(
        "SELECT COUNT(*) as c FROM leads WHERE role = ? AND whatsapp_sent = 1", (role,)
    ).fetchone()
    counts["whatsapp_sent_count"] = int(wa_row["c"] or 0) if wa_row else 0
    em_row = conn.execute(
        "SELECT COUNT(*) as c FROM leads WHERE role = ? AND email_sent = 1", (role,)
    ).fetchone()
    counts["email_sent_count"] = int(em_row["c"] or 0) if em_row else 0
    return counts


async def count_scheduled_callbacks_due_today(role: str) -> int:
    """Count leads with ``callback_scheduled`` status whose callback is due today or earlier."""
    return await asyncio.to_thread(_count_scheduled_callbacks_due_today_sync, role)

def _count_scheduled_callbacks_due_today_sync(role: str) -> int:
    conn = _get_conn()
    now = datetime.now(_dashboard_tz())
    tomorrow_start = datetime.combine(
        now.date() + timedelta(days=1),
        datetime.min.time(),
        tzinfo=_dashboard_tz(),
    ).timestamp()
    row = conn.execute(
        """
        SELECT COUNT(*) as c FROM leads
        WHERE role = ? AND status = 'callback_scheduled'
          AND json_extract(analysis, '$.callback_reminder_epoch') IS NOT NULL
          AND CAST(json_extract(analysis, '$.callback_reminder_epoch') AS REAL) > 0
          AND CAST(json_extract(analysis, '$.callback_reminder_epoch') AS REAL) < ?
          AND NOT EXISTS (
              SELECT 1 FROM scheduled_callbacks sc
              WHERE sc.lead_id = leads.id
                AND sc.status IN ('scheduled', 'queued', 'calling')
          )
        """,
        (role, tomorrow_start),
    ).fetchone()
    lead_due = int(row["c"] or 0) if row else 0
    row = conn.execute(
        """
        SELECT COUNT(*) as c FROM scheduled_callbacks
        WHERE role = ? AND status IN ('scheduled', 'queued', 'calling')
          AND scheduled_at > 0 AND scheduled_at < ?
        """,
        (role, tomorrow_start),
    ).fetchone()
    scheduled_due = int(row["c"] or 0) if row else 0
    return lead_due + scheduled_due


async def count_site_visit_followup_leads(role: str) -> int:
    return await asyncio.to_thread(_count_site_visit_followup_leads_sync, role)

def _count_site_visit_followup_leads_sync(role: str) -> int:
    conn = _get_conn()
    row = conn.execute(
        """
        SELECT COUNT(*) as c FROM leads 
        WHERE role = ? AND (
            status = 'site_visit'
            OR extra LIKE '%Assetz SV%'
            OR extra LIKE '%site_visit%'
            OR extra LIKE '%"sv"%'
            OR analysis LIKE '%site_visit%'
            OR analysis LIKE '%Site Visit%'
            OR analysis LIKE '%site visit%'
        )
        """,
        (role,)
    ).fetchone()
    return int(row["c"] or 0) if row else 0

async def get_site_visit_followup_leads(role: str, limit: int = 500) -> list[dict]:
    return await asyncio.to_thread(_get_site_visit_followup_leads_sync, role, limit)

def _get_site_visit_followup_leads_sync(role: str, limit: int = 500) -> list[dict]:
    conn = _get_conn()
    cursor = conn.execute(
        """
        SELECT * FROM leads 
        WHERE role = ? AND (
            status = 'site_visit'
            OR extra LIKE '%Assetz SV%'
            OR extra LIKE '%site_visit%'
            OR extra LIKE '%"sv"%'
            OR analysis LIKE '%site_visit%'
            OR analysis LIKE '%Site Visit%'
            OR analysis LIKE '%site visit%'
        )
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (role, limit),
    )
    return [dict(r) for r in cursor.fetchall()]


async def count_callbacks_completed_today(role: str) -> int:
    """Count leads that were completed (disposition set) today."""
    return await asyncio.to_thread(_count_callbacks_completed_today_sync, role)

def _count_callbacks_completed_today_sync(role: str) -> int:
    conn = _get_conn()
    now = datetime.now(_dashboard_tz())
    today_start = datetime.combine(
        now.date(),
        datetime.min.time(),
        tzinfo=_dashboard_tz(),
    ).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    row = conn.execute(
        """
        SELECT COUNT(*) as c FROM leads
        WHERE role = ? AND status IN ('callback_completed', 'completed')
          AND updated_at >= ?
          AND (
                json_extract(analysis, '$.callback_reminder_epoch') IS NOT NULL
             OR json_extract(analysis, '$.requested_callback_datetime_iso') IS NOT NULL
          )
          AND NOT EXISTS (
              SELECT 1 FROM scheduled_callbacks sc
              WHERE sc.lead_id = leads.id
                AND sc.status = 'completed'
                AND sc.updated_at >= ?
          )
        """,
        (role, today_start, today_start),
    ).fetchone()
    lead_completed = int(row["c"] or 0) if row else 0
    row = conn.execute(
        """
        SELECT COUNT(*) as c FROM scheduled_callbacks
        WHERE role = ? AND status = 'completed' AND updated_at >= ?
        """,
        (role, today_start),
    ).fetchone()
    scheduled_completed = int(row["c"] or 0) if row else 0
    return lead_completed + scheduled_completed


async def export_leads_csv(role: str, status_filter: str = "all") -> list[dict]:
    return await asyncio.to_thread(_export_leads_csv_sync, role, status_filter)

def _export_leads_csv_sync(role: str, status_filter: str = "all") -> list[dict]:
    conn = _get_conn()
    query = "SELECT id, name, phone, email, status, start_time, created_at, analysis, whatsapp_sent, email_sent, error, _call_id, _log_id FROM leads WHERE role = ?"
    params = [role]
    if status_filter != "all":
        filter_map = {
            "responded": "completed",
            "not_responded": "IN ('failed', 'pending', 'dialing')",
            "not_interested": "not_interested",
        }
        status_val = filter_map.get(status_filter, status_filter)
        if "IN" in status_val:
            query += f" AND status {status_val}"
        else:
            query += " AND status = ?"
            params.append(status_val)
    query += " ORDER BY created_at DESC"
    rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


async def find_lead_by_phone(role: str, raw_phone: str, status: Optional[str] = None) -> Optional[dict]:
    return await asyncio.to_thread(_find_lead_by_phone_sync, role, raw_phone, status)

def _find_lead_by_phone_sync(role: str, raw_phone: str, status: Optional[str] = None) -> Optional[dict]:
    """Match a lead row by normalized or last-10-digit phone for any campaign role.
    If *status* is provided, only match leads with that status.
    """
    from core.utils import _norm_phone_str

    role = (role or "sales_1").strip().lower()
    norm = _norm_phone_str(raw_phone or "")
    conn = _get_conn()

    def _query(where: str, params: tuple) -> Optional[dict]:
        if status:
            row = conn.execute(
                f"SELECT * FROM leads WHERE {where} AND status = ? ORDER BY updated_at DESC LIMIT 1",
                (*params, status),
            ).fetchone()
        else:
            row = conn.execute(
                f"SELECT * FROM leads WHERE {where} ORDER BY updated_at DESC LIMIT 1",
                params,
            ).fetchone()
        return _row_to_dict(row) if row else None

    if norm:
        result = _query("role = ? AND phone = ?", (role, norm))
        if result:
            return result
    digits = "".join(c for c in str(raw_phone or "") if c.isdigit())
    if len(digits) < 10:
        return None
    tail = digits[-10:]
    if len(tail) == 10:
        return _query("role = ? AND phone LIKE ?", (role, f"%{tail}"))
    return None


_CAMPAIGN_INBOUND_ROLES = ("sales_1",)


async def find_lead_by_phone_any_role(raw_phone: str) -> Optional[dict]:
    """Find the most recently updated campaign lead matching *raw_phone*."""
    return await asyncio.to_thread(_find_lead_by_phone_any_role_sync, raw_phone)


def _find_lead_by_phone_any_role_sync(raw_phone: str) -> Optional[dict]:
    for role in _CAMPAIGN_INBOUND_ROLES:
        row = _find_lead_by_phone_sync(role, raw_phone)
        if row:
            return row
    return None


async def record_inbound_whatsapp_reply(
    lead_id: int,
    *,
    reply_type: str,
    source: str,
    message_text: str,
    profile_name: str = "",
) -> dict[str, Any]:
    return await asyncio.to_thread(
        _record_inbound_whatsapp_reply_sync,
        lead_id, reply_type, source, message_text, profile_name,
    )


def _record_inbound_whatsapp_reply_sync(
    lead_id: int,
    reply_type: str,
    source: str,
    message_text: str,
    profile_name: str = "",
) -> dict[str, Any]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM leads WHERE id = ?", (int(lead_id),)).fetchone()
    if not row:
        return {"ok": False, "error": "lead not found"}
    lead = _row_to_dict(row)
    role = str(lead.get("role") or "")
    extra = coerce_extra_field(lead.get("extra"))
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    rtype = (reply_type or "interested").strip().lower()
    extra.update({
        "inbound_reply_type": rtype,
        "inbound_reply_at": now_iso,
        "inbound_reply_message": (message_text or "")[:2000],
        "inbound_interest_source": source,
        "whatsapp_last_message": (message_text or "")[:2000],
        "whatsapp_last_at": now_iso,
    })
    if rtype == "callback":
        extra["inbound_callback"] = True
        extra["inbound_callback_at"] = now_iso
        extra["inbound_interest"] = True  # show in same dashboard panel
        extra["inbound_interest_at"] = now_iso
        extra["inbound_interest_source"] = source or "whatsapp_callback"
        status_sql = """
            status = CASE
                WHEN status IN ('failed','not_interested','no answer','no response','busy','pending')
                THEN 'callback_scheduled'
                ELSE status
            END
        """
    else:
        extra["inbound_interest"] = True
        extra["inbound_interest_at"] = now_iso
        status_sql = """
            status = CASE
                WHEN status IN ('failed','not_interested','no answer','no response','busy')
                THEN 'interested'
                ELSE status
            END
        """
    if profile_name:
        extra["whatsapp_profile_name"] = profile_name.strip()

    conn.execute(
        f"""
        UPDATE leads SET
            extra = ?,
            {status_sql},
            lifecycle_status = ?,
            sandbox = ?,
            updated_at = datetime('now')
        WHERE id = ?
        """,
        (
            json.dumps(extra, ensure_ascii=False),
            "callback_requested" if rtype == "callback" else "interested",
            1 if rtype == "callback" else 3,
            int(lead_id),
        ),
    )
    # Any real WhatsApp response stops the no-reply automation. A callback
    # remains in Sandbox 1; an interested response is qualified for Sandbox 3.
    conn.execute(
        """UPDATE workflow_jobs
        SET status='cancelled', error='WhatsApp reply received', updated_at=datetime('now')
        WHERE lead_id=? AND job_type IN ('whatsapp_followup_24h','interested_followup')
          AND status IN ('scheduled','ready','claimed')""",
        (int(lead_id),),
    )
    conn.commit()
    _invalidate_state_cache()
    logger.info("Inbound WhatsApp reply recorded lead_id={} type={} source={}", lead_id, rtype, source)
    return {"ok": True, "lead_id": lead_id, "role": role, "source": source, "reply_type": rtype}


async def record_inbound_interest(
    lead_id: int,
    *,
    source: str,
    message_text: str,
    profile_name: str = "",
) -> dict[str, Any]:
    return await record_inbound_whatsapp_reply(
        lead_id,
        reply_type="interested",
        source=source,
        message_text=message_text,
        profile_name=profile_name,
    )


def _record_inbound_interest_sync(
    lead_id: int,
    source: str,
    message_text: str,
    profile_name: str = "",
) -> dict[str, Any]:
    return _record_inbound_whatsapp_reply_sync(
        lead_id, "interested", source, message_text, profile_name,
    )


async def get_inbound_interest_leads(role: str | None = None, limit: int = 100) -> list[dict]:
    return await asyncio.to_thread(_get_inbound_interest_leads_sync, role, limit)


def _get_inbound_interest_leads_sync(role: str | None = None, limit: int = 100) -> list[dict]:
    conn = _get_conn()
    params: list[Any] = []
    where = "(CAST(json_extract(extra, '$.inbound_interest') AS INTEGER) = 1 OR CAST(json_extract(extra, '$.inbound_callback') AS INTEGER) = 1)"
    if role:
        where += " AND role = ?"
        params.append(role.strip().lower())
    params.append(int(limit))
    rows = conn.execute(
        f"""
        SELECT id, role, name, phone, email, status, extra, analysis, updated_at,
               whatsapp_sent, email_sent, first_called_at, start_time
        FROM leads
        WHERE {where}
        ORDER BY COALESCE(
            json_extract(extra, '$.inbound_reply_at'),
            json_extract(extra, '$.inbound_interest_at'),
            json_extract(extra, '$.inbound_callback_at')
        ) DESC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


async def count_inbound_interest_leads(role: str | None = None) -> int:
    return await asyncio.to_thread(_count_inbound_interest_leads_sync, role)


def _count_inbound_interest_leads_sync(role: str | None = None) -> int:
    conn = _get_conn()
    if role:
        row = conn.execute(
            "SELECT COUNT(*) FROM leads WHERE role = ? AND (CAST(json_extract(extra, '$.inbound_interest') AS INTEGER) = 1 OR CAST(json_extract(extra, '$.inbound_callback') AS INTEGER) = 1)",
            (role.strip().lower(),),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) FROM leads WHERE CAST(json_extract(extra, '$.inbound_interest') AS INTEGER) = 1 OR CAST(json_extract(extra, '$.inbound_callback') AS INTEGER) = 1",
        ).fetchone()
    return int(row[0] if row else 0)


async def find_or_create_callback_lead(role: str, phone: str, name: str = "") -> int:
    """Reuse an existing lead row with ``callback_scheduled`` status for the same
    phone+role, or create a new one if none exists.

    Returns the lead ID (existing or newly created).
    """
    return await asyncio.to_thread(_find_or_create_callback_lead_sync, role, phone, name)

def _find_or_create_callback_lead_sync(role: str, phone: str, name: str = "") -> int:
    existing = _find_lead_by_phone_sync(role, phone, status="callback_scheduled")
    if existing:
        return existing["id"]
    existing = _find_lead_by_phone_sync(role, phone, status="failed")
    if existing:
        return existing["id"]
    existing = _find_lead_by_phone_sync(role, phone)
    if existing:
        return existing["id"]
    return _add_lead_sync(role, name or "Callback", phone)


# --- Sandbox Agents ---

async def create_agent(name: str, prompt: str, voice: str = "Puck", role: str = "factory") -> str:
    return await asyncio.to_thread(_create_agent_sync, name, prompt, voice, role)

def _create_agent_sync(name: str, prompt: str, voice: str = "Puck", role: str = "factory") -> str:
    import uuid
    agent_id = str(uuid.uuid4())
    conn = _get_conn()
    conn.execute(
        "INSERT INTO agents (id, role, name, prompt, voice) VALUES (?, ?, ?, ?, ?)",
        (agent_id, role, name, prompt, voice)
    )
    conn.commit()
    return agent_id


async def get_agent(agent_id: str) -> Optional[dict]:
    return await asyncio.to_thread(_get_agent_sync, agent_id)

def _get_agent_sync(agent_id: str) -> Optional[dict]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
    if not row:
        return None
    result = _row_to_dict(row)
    result["knowledge_files"] = json.loads(result.get("knowledge_files", "[]"))
    return result


async def list_agents(role: Optional[str] = None) -> list[dict]:
    return await asyncio.to_thread(_list_agents_sync, role)

def _list_agents_sync(role: Optional[str] = None) -> list[dict]:
    conn = _get_conn()
    if role:
        rows = conn.execute("SELECT * FROM agents WHERE role = ? ORDER BY created_at DESC", (role,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM agents ORDER BY created_at DESC").fetchall()
    result = []
    for row in rows:
        r = _row_to_dict(row)
        r["knowledge_files"] = json.loads(r.get("knowledge_files", "[]"))
        result.append(r)
    return result


async def update_agent(agent_id: str, name: str = None, prompt: str = None, voice: str = None):
    return await asyncio.to_thread(_update_agent_sync, agent_id, name, prompt, voice)

def _update_agent_sync(agent_id: str, name: str = None, prompt: str = None, voice: str = None):
    conn = _get_conn()
    updates = []
    params = []
    if name is not None:
        updates.append("name = ?")
        params.append(name)
    if prompt is not None:
        updates.append("prompt = ?")
        params.append(prompt)
    if voice is not None:
        updates.append("voice = ?")
        params.append(voice)
    if not updates:
        return
    updates.append("updated_at = datetime('now')")
    params.append(agent_id)
    conn.execute(f"UPDATE agents SET {', '.join(updates)} WHERE id = ?", params)
    conn.commit()


async def delete_agent(agent_id: str) -> bool:
    return await asyncio.to_thread(_delete_agent_sync, agent_id)

def _delete_agent_sync(agent_id: str) -> bool:
    conn = _get_conn()
    cur = conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
    conn.commit()
    return cur.rowcount > 0


async def add_agent_knowledge_file(agent_id: str, file_id: str, filename: str, extracted_text: str):
    return await asyncio.to_thread(_add_agent_knowledge_file_sync, agent_id, file_id, filename, extracted_text)


# ─── Prompt Versioning ───

async def save_prompt_version(
    role: str, prompt: str, rag: str = "", greeting_text: str = "",
    status: str = "active", notes: str = "", created_by: str = "admin",
) -> int:
    return await asyncio.to_thread(
        _save_prompt_version_sync, role, prompt, rag, greeting_text, status, notes, created_by
    )

def _save_prompt_version_sync(
    role: str, prompt: str, rag: str = "", greeting_text: str = "",
    status: str = "active", notes: str = "", created_by: str = "admin",
) -> int:
    conn = _get_conn()
    # Deactivate any currently active versions for this role
    if status == "active":
        conn.execute(
            "UPDATE prompt_versions SET status = 'archived' WHERE role = ? AND status = 'active'",
            (role,),
        )
    # Determine next version number
    row = conn.execute(
        "SELECT COALESCE(MAX(version_number), 0) + 1 FROM prompt_versions WHERE role = ?",
        (role,),
    ).fetchone()
    next_ver = row[0] if row else 1
    conn.execute(
        "INSERT INTO prompt_versions (role, prompt, rag, greeting_text, version_number, status, created_by, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (role, prompt, rag, greeting_text, next_ver, status, created_by, notes),
    )
    conn.commit()
    return next_ver


async def get_prompt_versions(role: str, limit: int = 20) -> list[dict]:
    return await asyncio.to_thread(_get_prompt_versions_sync, role, limit)

def _get_prompt_versions_sync(role: str, limit: int = 20) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM prompt_versions WHERE role = ? ORDER BY version_number DESC LIMIT ?",
        (role, limit),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


async def get_active_prompt_version(role: str) -> dict | None:
    return await asyncio.to_thread(_get_active_prompt_version_sync, role)

def _get_active_prompt_version_sync(role: str) -> dict | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM prompt_versions WHERE role = ? AND status = 'active' ORDER BY version_number DESC LIMIT 1",
        (role,),
    ).fetchone()
    return _row_to_dict(row) if row else None


async def restore_prompt_version(role: str, version_id: int) -> dict | None:
    return await asyncio.to_thread(_restore_prompt_version_sync, role, version_id)

def _restore_prompt_version_sync(role: str, version_id: int) -> dict | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM prompt_versions WHERE id = ? AND role = ?", (version_id, role)
    ).fetchone()
    if not row:
        return None
    target = _row_to_dict(row)
    # Deactivate current active
    conn.execute(
        "UPDATE prompt_versions SET status = 'archived' WHERE role = ? AND status = 'active'",
        (role,),
    )
    # Activate the target version
    conn.execute(
        "UPDATE prompt_versions SET status = 'active' WHERE id = ?", (version_id,)
    )
    # Also update role_state and prompt files so the next call picks it up
    from core.state import save_role_state
    from prompts.role_prompts import set_role_prompt_text, set_role_rag_source_text

    save_role_state(
        role,
        prompt=target.get("prompt", ""),
        rag=target.get("rag", ""),
        greeting_text=target.get("greeting_text", ""),
    )
    set_role_prompt_text(role, target.get("prompt", ""))
    set_role_rag_source_text(role, target.get("rag", ""))

    # Invalidate cache
    try:
        from core import kv_cache
        kv_cache.invalidate_role(role)
    except Exception:
        pass

    # Rebuild KB chunks from the restored RAG so the next call does not serve
    # chunks from the previous version (mirrors update_tuning in console_api).
    try:
        from services.chunk_rag import rebuild_role_kb_chunks
        rebuild_role_kb_chunks(role)
    except Exception:
        pass

    conn.commit()
    return target

def _add_agent_knowledge_file_sync(agent_id: str, file_id: str, filename: str, extracted_text: str):
    conn = _get_conn()
    row = conn.execute("SELECT knowledge_files FROM agents WHERE id = ?", (agent_id,)).fetchone()
    if not row:
        return
    files = json.loads(row["knowledge_files"] or "[]")
    files.append({
        "file_id": file_id,
        "filename": filename,
        "extracted_text": extracted_text,
        "added_at": datetime.now(timezone.utc).isoformat(),
    })
    conn.execute(
        "UPDATE agents SET knowledge_files = ?, updated_at = datetime('now') WHERE id = ?",
        (json.dumps(files), agent_id)
    )
    conn.commit()


async def add_agent_lead(agent_id: str, lead: dict) -> str:
    return await asyncio.to_thread(_add_agent_lead_sync, agent_id, lead)

def _add_agent_lead_sync(agent_id: str, lead: dict) -> str:
    import uuid
    lead_id = str(uuid.uuid4())
    conn = _get_conn()
    conn.execute(
        "INSERT INTO agent_leads (agent_id, lead_id, name, phone, email, company) VALUES (?, ?, ?, ?, ?, ?)",
        (agent_id, lead_id, lead.get("name", "Unknown"), lead.get("phone", ""), lead.get("email", ""), lead.get("company", ""))
    )
    conn.commit()
    return lead_id


async def get_agent_leads(agent_id: str) -> list[dict]:
    return await asyncio.to_thread(_get_agent_leads_sync, agent_id)

def _get_agent_leads_sync(agent_id: str) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM agent_leads WHERE agent_id = ? ORDER BY created_at DESC",
        (agent_id,)
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


# --- Campaign Cases ---

async def list_cases(role: str) -> list[dict]:
    return await asyncio.to_thread(_list_cases_sync, role)

def _list_cases_sync(role: str) -> list[dict]:
    """All cases for a role, newest first. Each row is a plain dict."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, role, name, description, active, created_at, updated_at "
        "FROM cases WHERE role = ? ORDER BY active DESC, created_at DESC",
        (role,),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        d = _row_to_dict(r)
        d["active"] = bool(d.get("active"))
        out.append(d)
    return out


from typing import Optional, Union, List

async def get_active_case(role: str) -> Optional[dict]:
    return await asyncio.to_thread(_get_active_case_sync, role)

def _get_active_case_sync(role: str) -> Optional[dict]:
    """Return the (single) active case for a role, or None."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT id, role, name, description, active, created_at, updated_at "
        "FROM cases WHERE role = ? AND active = 1 LIMIT 1",
        (role,),
    ).fetchone()
    if not row:
        return None
    d = _row_to_dict(row)
    d["active"] = True
    return d


async def add_case(role: str, name: str, description: str = "") -> int:
    return await asyncio.to_thread(_add_case_sync, role, name, description)

def _add_case_sync(role: str, name: str, description: str = "") -> int:
    """Insert a new case (inactive by default). Returns the new id."""
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO cases (role, name, description, active) VALUES (?, ?, ?, 0)",
        (role, name.strip(), description or ""),
    )
    conn.commit()
    return int(cur.lastrowid)


async def update_case(case_id: int, name: Optional[str] = None, description: Optional[str] = None) -> bool:
    return await asyncio.to_thread(_update_case_sync, case_id, name, description)

def _update_case_sync(case_id: int, name: Optional[str] = None, description: Optional[str] = None) -> bool:
    """Update a case's name and/or description. Returns True if a row changed."""
    conn = _get_conn()
    sets: list[str] = []
    params: list = []
    if name is not None:
        sets.append("name = ?")
        params.append(name.strip())
    if description is not None:
        sets.append("description = ?")
        params.append(description)
    if not sets:
        return False
    sets.append("updated_at = datetime('now')")
    params.append(case_id)
    cur = conn.execute(
        f"UPDATE cases SET {', '.join(sets)} WHERE id = ?",
        params,
    )
    conn.commit()
    return cur.rowcount > 0


async def delete_case(case_id: int) -> bool:
    return await asyncio.to_thread(_delete_case_sync, case_id)

def _delete_case_sync(case_id: int) -> bool:
    """Delete a case. Returns True if a row was removed."""
    conn = _get_conn()
    cur = conn.execute("DELETE FROM cases WHERE id = ?", (case_id,))
    conn.commit()
    return cur.rowcount > 0


async def set_active_case(role: str, case_id: Optional[int]) -> bool:
    return await asyncio.to_thread(_set_active_case_sync, role, case_id)

def _set_active_case_sync(role: str, case_id: Optional[int]) -> bool:
    """Activate exactly one case for ``role`` (or none if ``case_id`` is None).

    Always deactivates any currently-active case for that role first so the
    invariant "at most one active case per role" cannot be violated.
    """
    conn = _get_conn()
    conn.execute(
        "UPDATE cases SET active = 0, updated_at = datetime('now') "
        "WHERE role = ? AND active = 1",
        (role,),
    )
    if case_id is None:
        conn.commit()
        return True
    cur = conn.execute(
        "UPDATE cases SET active = 1, updated_at = datetime('now') "
        "WHERE id = ? AND role = ?",
        (case_id, role),
    )
    conn.commit()
    return cur.rowcount > 0


# --- Campaign Schedules ---

# Allowed status transitions:
#   scheduled -> running | cancelled | failed
#   running   -> completed | failed
# Anything else is a bug; we still let the row update but the API/UI never
# surfaces those transitions.
_SCHEDULE_VALID_STATUSES = {
    "scheduled", "running", "completed", "failed", "cancelled",
}


async def add_schedule(
    role: str,
    run_at: float,
    name: str = "",
    stop_at: float | None = None,
) -> int:
    return await asyncio.to_thread(_add_schedule_sync, role, run_at, name, stop_at)

def _add_schedule_sync(
    role: str,
    run_at: float,
    name: str = "",
    stop_at: float | None = None,
) -> int:
    """Schedule a campaign run for ``role`` at epoch ``run_at`` (UTC seconds).

    If ``stop_at`` (also epoch-UTC) is given, the worker auto-stops the campaign
    at that moment — useful for "run from 9 AM to 5 PM only" windows.

    Returns the new schedule id. ``name`` is an optional human label
    (e.g. "Friday morning blast").
    """
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO schedules (role, name, run_at, stop_at, status) "
        "VALUES (?, ?, ?, ?, 'scheduled')",
        (
            role,
            (name or "").strip(),
            float(run_at),
            float(stop_at) if stop_at is not None else None,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


# Column list re-used across SELECTs so adding fields stays a one-line change.
_SCHEDULE_COLS = (
    "id, role, name, run_at, stop_at, status, "
    "created_at, updated_at, started_at, error"
)


async def list_schedules(role: str, limit: int = 100) -> list[dict]:
    return await asyncio.to_thread(_list_schedules_sync, role, limit)

def _list_schedules_sync(role: str, limit: int = 100) -> list[dict]:
    """All schedules for ``role``, soonest first (active/scheduled on top)."""
    conn = _get_conn()
    rows = conn.execute(
        f"SELECT {_SCHEDULE_COLS} FROM schedules WHERE role = ? "
        "ORDER BY CASE status "
        "    WHEN 'running'   THEN 0 "
        "    WHEN 'scheduled' THEN 1 "
        "    ELSE 2 END, "
        "run_at ASC LIMIT ?",
        (role, int(limit)),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


async def get_schedule(schedule_id: int) -> dict | None:
    return await asyncio.to_thread(_get_schedule_sync, schedule_id)

def _get_schedule_sync(schedule_id: int) -> dict | None:
    conn = _get_conn()
    row = conn.execute(
        f"SELECT {_SCHEDULE_COLS} FROM schedules WHERE id = ?",
        (schedule_id,),
    ).fetchone()
    return _row_to_dict(row) if row else None


async def cancel_schedule(schedule_id: int) -> bool:
    return await asyncio.to_thread(_cancel_schedule_sync, schedule_id)

def _cancel_schedule_sync(schedule_id: int) -> bool:
    """Mark a *scheduled* (not-yet-started) run as cancelled. Returns True on success."""
    conn = _get_conn()
    cur = conn.execute(
        "UPDATE schedules SET status = 'cancelled', updated_at = datetime('now') "
        "WHERE id = ? AND status = 'scheduled'",
        (schedule_id,),
    )
    conn.commit()
    return cur.rowcount > 0


async def mark_schedule_status(
    schedule_id: int,
    status: str,
    error: str | None = None,
    started_at: float | None = None,
) -> bool:
    return await asyncio.to_thread(_mark_schedule_status_sync, schedule_id, status, error, started_at)

def _mark_schedule_status_sync(
    schedule_id: int,
    status: str,
    error: str | None = None,
    started_at: float | None = None,
) -> bool:
    """Update a schedule's lifecycle status. Returns True if a row changed."""
    if status not in _SCHEDULE_VALID_STATUSES:
        return False
    conn = _get_conn()
    sets = ["status = ?", "updated_at = datetime('now')"]
    params: list = [status]
    if error is not None:
        sets.append("error = ?")
        params.append(error)
    if started_at is not None:
        sets.append("started_at = ?")
        params.append(float(started_at))
    params.append(schedule_id)
    cur = conn.execute(
        f"UPDATE schedules SET {', '.join(sets)} WHERE id = ?",
        params,
    )
    conn.commit()
    return cur.rowcount > 0


async def due_schedules(now_epoch: float, lookahead_sec: float = 0.0) -> list[dict]:
    return await asyncio.to_thread(_due_schedules_sync, now_epoch, lookahead_sec)

def _due_schedules_sync(now_epoch: float, lookahead_sec: float = 0.0) -> list[dict]:
    """All schedules that are eligible to fire at ``now_epoch``.

    ``lookahead_sec`` is for callers that want to peek slightly in the future
    (e.g. to warn the user). The worker always passes 0.
    """
    conn = _get_conn()
    rows = conn.execute(
        f"SELECT {_SCHEDULE_COLS} FROM schedules "
        "WHERE status = 'scheduled' AND run_at <= ? ORDER BY run_at ASC",
        (float(now_epoch) + float(lookahead_sec),),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


async def expired_running_schedules(now_epoch: float) -> list[dict]:
    return await asyncio.to_thread(_expired_running_schedules_sync, now_epoch)

def _expired_running_schedules_sync(now_epoch: float) -> list[dict]:
    """All ``running`` schedules whose ``stop_at`` has passed.

    Used by the scheduler loop to enforce the auto-stop window even after a
    server restart (which would have orphaned the inline stop watcher).
    """
    conn = _get_conn()
    rows = conn.execute(
        f"SELECT {_SCHEDULE_COLS} FROM schedules "
        "WHERE status = 'running' AND stop_at IS NOT NULL AND stop_at <= ? "
        "ORDER BY stop_at ASC",
        (float(now_epoch),),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


# --- Manual calls (console "Make a Call") ---


async def insert_manual_call(role: str, camp_id: str, to_phone: str, callee_name: str) -> int:
    return await asyncio.to_thread(_insert_manual_call_sync, role, camp_id, to_phone, callee_name)

def _insert_manual_call_sync(role: str, camp_id: str, to_phone: str, callee_name: str) -> int:
    conn = _get_conn()
    cur = conn.execute(
        """
        INSERT INTO manual_calls (role, camp_id, to_phone, callee_name, status)
        VALUES (?, ?, ?, ?, 'dialing')
        """,
        (role, camp_id, to_phone or "", callee_name or ""),
    )
    conn.commit()
    return int(cur.lastrowid)


async def mark_manual_call_failed(camp_id: str, message: str = "") -> None:
    return await asyncio.to_thread(_mark_manual_call_failed_sync, camp_id, message)

def _mark_manual_call_failed_sync(camp_id: str, message: str = "") -> None:
    conn = _get_conn()
    conn.execute(
        """
        UPDATE manual_calls SET status = 'failed', error = ?, updated_at = datetime('now')
        WHERE camp_id = ? AND status != 'completed'
        """,
        ((message or "")[:2000], camp_id),
    )
    conn.commit()


async def manual_call_row_by_camp_id(camp_id: str) -> Optional[dict]:
    return await asyncio.to_thread(_manual_call_row_by_camp_id_sync, camp_id)

def _manual_call_row_by_camp_id_sync(camp_id: str) -> Optional[dict]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM manual_calls WHERE camp_id = ?", (camp_id,)).fetchone()
    return dict(row) if row else None


async def lead_row_by_call_id(call_id: str) -> Optional[dict]:
    return await asyncio.to_thread(_lead_row_by_call_id_sync, call_id)


def _lead_row_by_call_id_sync(call_id: str) -> Optional[dict]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM leads WHERE _call_id = ?", (call_id,)).fetchone()
    return dict(row) if row else None


async def upsert_camp_session(camp_id: str, role: str, payload: dict) -> None:
    return await asyncio.to_thread(_upsert_camp_session_sync, camp_id, role, payload)


def upsert_vobiz_call_map(
    *,
    call_uuid: str,
    camp_id: str = "",
    log_id: str = "",
    role: str = "",
    phone: str = "",
    auth_id: str = "",
) -> None:
    """Persist CallUUID → camp/log mapping (sync; safe from webhook threads)."""
    cu = (call_uuid or "").strip()
    if not cu:
        return
    try:
        conn = _get_conn()
        existing = conn.execute(
            "SELECT camp_id, log_id, role, phone, auth_id FROM vobiz_call_map WHERE call_uuid = ?",
            (cu.lower(),),
        ).fetchone()
        if existing:
            camp_id = camp_id or (existing["camp_id"] or "")
            log_id = log_id or (existing["log_id"] or "")
            role = role or (existing["role"] or "")
            phone = phone or (existing["phone"] or "")
            auth_id = auth_id or (existing["auth_id"] or "")
        conn.execute(
            """
            INSERT INTO vobiz_call_map
                (call_uuid, camp_id, log_id, role, phone, auth_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
            ON CONFLICT(call_uuid) DO UPDATE SET
                camp_id = excluded.camp_id,
                log_id = excluded.log_id,
                role = excluded.role,
                phone = excluded.phone,
                auth_id = excluded.auth_id,
                updated_at = datetime('now')
            """,
            (
                cu.lower(),
                (camp_id or "").strip(),
                (log_id or "").strip(),
                (role or "").strip(),
                (phone or "").strip(),
                (auth_id or "").strip(),
            ),
        )
        conn.commit()
    except Exception as exc:
        logger.warning("upsert_vobiz_call_map failed call_uuid={}: {}", cu[:36], exc)


def lookup_vobiz_call_map(call_uuid: str) -> dict:
    cu = (call_uuid or "").strip().lower()
    if not cu:
        return {}
    try:
        conn = _get_conn()
        row = conn.execute(
            "SELECT call_uuid, camp_id, log_id, role, phone, auth_id FROM vobiz_call_map WHERE call_uuid = ?",
            (cu,),
        ).fetchone()
        if not row:
            return {}
        return {
            "call_uuid": str(row["call_uuid"] or ""),
            "camp_id": str(row["camp_id"] or ""),
            "log_id": str(row["log_id"] or ""),
            "role": str(row["role"] or ""),
            "phone": str(row["phone"] or ""),
            "auth_id": str(row["auth_id"] or ""),
        }
    except Exception:
        return {}


def lookup_vobiz_call_map_by_log_id(log_id: str) -> dict:
    target = (log_id or "").strip()
    if not target:
        return {}
    try:
        conn = _get_conn()
        row = conn.execute(
            """
            SELECT call_uuid, camp_id, log_id, role, phone, auth_id
            FROM vobiz_call_map
            WHERE log_id = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (target,),
        ).fetchone()
        if not row:
            return {}
        return {
            "call_uuid": str(row["call_uuid"] or ""),
            "camp_id": str(row["camp_id"] or ""),
            "log_id": str(row["log_id"] or ""),
            "role": str(row["role"] or ""),
            "phone": str(row["phone"] or ""),
            "auth_id": str(row["auth_id"] or ""),
        }
    except Exception:
        return {}


def list_pending_vobiz_recording_targets(hours: int = 24, limit: int = 40) -> list[dict]:
    """Recent ended calls that have log_id but may still need Vobiz ingest."""
    hours = max(1, min(int(hours), 168))
    limit = max(1, min(int(limit), 200))
    out: list[dict] = []
    try:
        conn = _get_conn()
        for table, camp_col in (
            ("manual_calls", "camp_id"),
            ("incoming_calls", "camp_id"),
        ):
            rows = conn.execute(
                f"""
                SELECT id, role, {camp_col} AS camp_id, log_id, status, ended_at
                FROM {table}
                WHERE log_id IS NOT NULL AND TRIM(log_id) != ''
                  AND ended_at IS NOT NULL AND TRIM(ended_at) != ''
                  AND datetime(ended_at) >= datetime('now', ?)
                ORDER BY id DESC
                LIMIT ?
                """,
                (f"-{hours} hours", limit),
            ).fetchall()
            for row in rows:
                out.append(
                    {
                        "source": table,
                        "id": row["id"],
                        "role": row["role"],
                        "camp_id": row["camp_id"],
                        "log_id": row["log_id"],
                        "status": row["status"],
                        "ended_at": row["ended_at"],
                    }
                )
        # Campaign call_attempts from last N hours
        ca_rows = conn.execute(
            """
            SELECT id, role, lead_id, log_id, status, created_at
            FROM call_attempts
            WHERE log_id IS NOT NULL AND TRIM(log_id) != ''
              AND datetime(created_at) >= datetime('now', ?)
            ORDER BY id DESC
            LIMIT ?
            """,
            (f"-{hours} hours", limit),
        ).fetchall()
        for row in ca_rows:
            out.append(
                {
                    "source": "call_attempts",
                    "id": row["id"],
                    "role": row["role"],
                    "camp_id": "",
                    "log_id": row["log_id"],
                    "status": row["status"],
                    "ended_at": row["created_at"],
                    "lead_id": row["lead_id"],
                }
            )
    except Exception as exc:
        logger.warning("list_pending_vobiz_recording_targets failed: {}", exc)
    return out


def _upsert_camp_session_sync(camp_id: str, role: str, payload: dict) -> None:
    conn = _get_conn()
    conn.execute(
        """
        INSERT INTO camp_sessions (camp_id, role, payload_json, updated_at)
        VALUES (?, ?, ?, datetime('now'))
        ON CONFLICT(camp_id) DO UPDATE SET
            role = excluded.role,
            payload_json = excluded.payload_json,
            updated_at = datetime('now')
        """,
        (camp_id, role, json.dumps(payload or {}, ensure_ascii=False)),
    )
    conn.commit()


async def get_camp_session(camp_id: str) -> Optional[dict]:
    return await asyncio.to_thread(_get_camp_session_sync, camp_id)


def _get_camp_session_sync(camp_id: str) -> Optional[dict]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM camp_sessions WHERE camp_id = ?", (camp_id,)).fetchone()
    return dict(row) if row else None


async def update_camp_session_connected(
    camp_id: str,
    connected_at: float,
    log_id: Optional[str] = None,
    role: str = "",
) -> None:
    return await asyncio.to_thread(
        _update_camp_session_connected_sync, camp_id, connected_at, log_id, role
    )


def _update_camp_session_connected_sync(
    camp_id: str,
    connected_at: float,
    log_id: Optional[str] = None,
    role: str = "",
) -> None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT camp_id FROM camp_sessions WHERE camp_id = ?",
        (camp_id,),
    ).fetchone()
    if not row:
        conn.execute(
            """
            INSERT INTO camp_sessions (camp_id, role, payload_json, connected_at, log_id, updated_at)
            VALUES (?, ?, '{}', ?, ?, datetime('now'))
            """,
            (camp_id, (role or "")[:80], float(connected_at), (log_id or "")[:200] if log_id else None),
        )
    elif log_id:
        conn.execute(
            """
            UPDATE camp_sessions
            SET connected_at = ?, log_id = ?, updated_at = datetime('now')
            WHERE camp_id = ?
            """,
            (float(connected_at), str(log_id)[:200], camp_id),
        )
    else:
        conn.execute(
            """
            UPDATE camp_sessions
            SET connected_at = ?, updated_at = datetime('now')
            WHERE camp_id = ?
            """,
            (float(connected_at), camp_id),
        )
    conn.commit()


async def update_camp_session_ended(camp_id: str, ended_at: float, role: str = "") -> None:
    return await asyncio.to_thread(_update_camp_session_ended_sync, camp_id, ended_at, role)


def _update_camp_session_ended_sync(camp_id: str, ended_at: float, role: str = "") -> None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT camp_id FROM camp_sessions WHERE camp_id = ?",
        (camp_id,),
    ).fetchone()
    if not row:
        conn.execute(
            """
            INSERT INTO camp_sessions (camp_id, role, payload_json, ended_at, updated_at)
            VALUES (?, ?, '{}', ?, datetime('now'))
            """,
            (camp_id, (role or "")[:80], float(ended_at)),
        )
    else:
        conn.execute(
            """
            UPDATE camp_sessions
            SET ended_at = ?, updated_at = datetime('now')
            WHERE camp_id = ?
            """,
            (float(ended_at), camp_id),
        )
    conn.commit()


async def manual_call_exists_for_camp(camp_id: str) -> bool:
    return await asyncio.to_thread(_manual_call_exists_for_camp_sync, camp_id)

def _manual_call_exists_for_camp_sync(camp_id: str) -> bool:
    conn = _get_conn()
    row = conn.execute("SELECT 1 FROM manual_calls WHERE camp_id = ?", (camp_id,)).fetchone()
    return row is not None


async def finalize_manual_call_record(
    camp_id: str,
    log_id: str,
    duration_sec: Optional[float],
    analysis: dict[str, Any],
) -> None:
    return await asyncio.to_thread(_finalize_manual_call_record_sync, camp_id, log_id, duration_sec, analysis)

def _finalize_manual_call_record_sync(
    camp_id: str,
    log_id: str,
    duration_sec: Optional[float],
    analysis: dict[str, Any],
) -> None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT id, status FROM manual_calls WHERE camp_id = ?",
        (camp_id,),
    ).fetchone()
    if not row or (row["status"] or "") == "completed":
        return
    aj = json.dumps(analysis, ensure_ascii=False)
    conf = analysis.get("emotion_confidence")
    try:
        conf_f = float(conf) if conf is not None and str(conf).strip() != "" else None
    except (TypeError, ValueError):
        conf_f = None
    conn.execute(
        """
        UPDATE manual_calls SET
            log_id = ?,
            status = 'completed',
            ended_at = datetime('now'),
            duration_sec = ?,
            disposition = ?,
            summary = ?,
            next_steps = ?,
            emotion_label = ?,
            emotion_rationale = ?,
            emotion_confidence = ?,
            analysis_json = ?,
            updated_at = datetime('now')
        WHERE camp_id = ?
        """,
        (
            log_id or "",
            duration_sec,
            str(analysis.get("disposition") or ""),
            str(analysis.get("summary") or ""),
            str(analysis.get("next_steps") or ""),
            str(analysis.get("emotion_label") or ""),
            str(analysis.get("emotion_rationale") or ""),
            conf_f,
            aj,
            camp_id,
        ),
    )
    conn.commit()


async def update_manual_call_analysis_by_id(call_id: int, analysis: dict[str, Any]) -> bool:
    return await asyncio.to_thread(_update_manual_call_analysis_by_id_sync, call_id, analysis)

def _update_manual_call_analysis_by_id_sync(call_id: int, analysis: dict[str, Any]) -> bool:
    """Rewrite analyzer fields on a manual_calls row (e.g. Re-analyze button)."""
    conn = _get_conn()
    row = conn.execute("SELECT id FROM manual_calls WHERE id = ?", (int(call_id),)).fetchone()
    if not row:
        return False
    aj = json.dumps(analysis, ensure_ascii=False)
    conf = analysis.get("emotion_confidence")
    try:
        conf_f = float(conf) if conf is not None and str(conf).strip() != "" else None
    except (TypeError, ValueError):
        conf_f = None
    conn.execute(
        """
        UPDATE manual_calls SET
            disposition = ?,
            summary = ?,
            next_steps = ?,
            emotion_label = ?,
            emotion_rationale = ?,
            emotion_confidence = ?,
            analysis_json = ?,
            error = '',
            updated_at = datetime('now')
        WHERE id = ?
        """,
        (
            str(analysis.get("disposition") or ""),
            str(analysis.get("summary") or ""),
            str(analysis.get("next_steps") or ""),
            str(analysis.get("emotion_label") or ""),
            str(analysis.get("emotion_rationale") or ""),
            conf_f,
            aj,
            int(call_id),
        ),
    )
    conn.commit()
    return True


async def get_manual_call_by_id(call_id: int) -> Optional[dict]:
    return await asyncio.to_thread(_get_manual_call_by_id_sync, call_id)

def _get_manual_call_by_id_sync(call_id: int) -> Optional[dict]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM manual_calls WHERE id = ?", (int(call_id),)).fetchone()
    return dict(row) if row else None


async def list_recent_manual_calls(role: str, limit: int = 15) -> list[dict]:
    return await asyncio.to_thread(_list_recent_manual_calls_sync, role, limit)

def _list_recent_manual_calls_sync(role: str, limit: int = 15) -> list[dict]:
    conn = _get_conn()
    lim = max(1, min(int(limit), 50))
    rows = conn.execute(
        """
        SELECT * FROM manual_calls WHERE role = ?
        ORDER BY id DESC LIMIT ?
        """,
        (role, lim),
    ).fetchall()
    return [dict(r) for r in rows]


# --- Incoming calls (customer call-backs) ---


async def insert_incoming_call(
    role: str,
    camp_id: str,
    from_phone: str,
    caller_name: str,
    *,
    status: str = "ringing",
    to_phone: str = "",
) -> int:
    return await asyncio.to_thread(
        _insert_incoming_call_sync,
        role,
        camp_id,
        from_phone,
        caller_name,
        status,
        to_phone,
    )


def _insert_incoming_call_sync(
    role: str,
    camp_id: str,
    from_phone: str,
    caller_name: str,
    status: str = "ringing",
    to_phone: str = "",
) -> int:
    conn = _get_conn()
    cur = conn.execute(
        """
        INSERT INTO incoming_calls (role, camp_id, from_phone, caller_name, status, to_phone)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            role,
            camp_id,
            from_phone or "",
            caller_name or "",
            (status or "ringing").strip(),
            to_phone or "",
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


async def list_missed_busy_incoming_pending(limit: int = 25) -> list[dict]:
    return await asyncio.to_thread(_list_missed_busy_incoming_pending_sync, limit)


def _list_missed_busy_incoming_pending_sync(limit: int = 25) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        """
        SELECT * FROM incoming_calls
        WHERE status = 'missed_busy' AND COALESCE(callback_scheduled, 0) = 0
        ORDER BY id ASC
        LIMIT ?
        """,
        (max(1, int(limit)),),
    ).fetchall()
    return [dict(r) for r in rows]


async def mark_incoming_callback_scheduled(call_id: int) -> None:
    return await asyncio.to_thread(_mark_incoming_callback_scheduled_sync, call_id)


def _mark_incoming_callback_scheduled_sync(call_id: int) -> None:
    conn = _get_conn()
    conn.execute(
        """
        UPDATE incoming_calls
        SET callback_scheduled = 1, updated_at = datetime('now')
        WHERE id = ?
        """,
        (int(call_id),),
    )
    conn.commit()


async def mark_incoming_call_failed(camp_id: str, message: str = "") -> None:
    return await asyncio.to_thread(_mark_incoming_call_failed_sync, camp_id, message)


def _mark_incoming_call_failed_sync(camp_id: str, message: str = "") -> None:
    conn = _get_conn()
    conn.execute(
        """
        UPDATE incoming_calls SET status = 'failed', error = ?, updated_at = datetime('now')
        WHERE camp_id = ? AND status != 'completed'
        """,
        ((message or "")[:2000], camp_id),
    )
    conn.commit()


def _inbound_counts_on_calendar_dates_sync(role: str, iso_dates: list[str]) -> dict[str, int]:
    """Return ``{YYYY-MM-DD: count}`` incoming-call rows on those calendar dates."""

    from core.state import normalize_console_role

    role = normalize_console_role(role)
    if not iso_dates:
        return {}
    placeholders = ",".join("?" * len(iso_dates))
    conn = _get_conn()
    rows = conn.execute(
        f"""
        SELECT date(started_at) AS d, COUNT(*) AS c
        FROM incoming_calls
        WHERE role = ? AND date(started_at) IN ({placeholders})
        GROUP BY date(started_at)
        """,
        (role, *tuple(iso_dates)),
    ).fetchall()
    return {str(r["d"]): int(r["c"]) for r in rows if r["d"] is not None}


async def update_incoming_call_status(camp_id: str, status: str) -> None:
    return await asyncio.to_thread(_update_incoming_call_status_sync, camp_id, status)


async def update_incoming_call_on_connect(camp_id: str, log_id: str) -> None:
    return await asyncio.to_thread(_update_incoming_call_on_connect_sync, camp_id, log_id)


def _update_incoming_call_on_connect_sync(camp_id: str, log_id: str) -> None:
    conn = _get_conn()
    conn.execute(
        """
        UPDATE incoming_calls
        SET log_id = ?, status = 'connected', updated_at = datetime('now')
        WHERE camp_id = ? AND status != 'completed'
        """,
        (log_id or "", camp_id),
    )
    conn.commit()


def _update_incoming_call_status_sync(camp_id: str, status: str) -> None:
    conn = _get_conn()
    conn.execute(
        "UPDATE incoming_calls SET status = ?, updated_at = datetime('now') WHERE camp_id = ? AND status != 'completed'",
        (status, camp_id),
    )
    conn.commit()


async def incoming_call_row_by_camp_id(camp_id: str) -> Optional[dict]:
    return await asyncio.to_thread(_incoming_call_row_by_camp_id_sync, camp_id)


def _incoming_call_row_by_camp_id_sync(camp_id: str) -> Optional[dict]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM incoming_calls WHERE camp_id = ?", (camp_id,)).fetchone()
    return dict(row) if row else None


async def finalize_incoming_call_record(
    camp_id: str,
    log_id: str,
    duration_sec: Optional[float],
    analysis: dict[str, Any],
) -> None:
    return await asyncio.to_thread(_finalize_incoming_call_record_sync, camp_id, log_id, duration_sec, analysis)


def _finalize_incoming_call_record_sync(
    camp_id: str,
    log_id: str,
    duration_sec: Optional[float],
    analysis: dict[str, Any],
) -> None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT id, status FROM incoming_calls WHERE camp_id = ?",
        (camp_id,),
    ).fetchone()
    if not row or (row["status"] or "") == "completed":
        return
    aj = json.dumps(analysis, ensure_ascii=False)
    conf = analysis.get("emotion_confidence")
    try:
        conf_f = float(conf) if conf is not None and str(conf).strip() != "" else None
    except (TypeError, ValueError):
        conf_f = None
    conn.execute(
        """
        UPDATE incoming_calls SET
            log_id = ?,
            status = 'completed',
            ended_at = datetime('now'),
            duration_sec = ?,
            disposition = ?,
            summary = ?,
            next_steps = ?,
            emotion_label = ?,
            emotion_rationale = ?,
            emotion_confidence = ?,
            analysis_json = ?,
            updated_at = datetime('now')
        WHERE camp_id = ?
        """,
        (
            log_id or "",
            duration_sec,
            str(analysis.get("disposition") or ""),
            str(analysis.get("summary") or ""),
            str(analysis.get("next_steps") or ""),
            str(analysis.get("emotion_label") or ""),
            str(analysis.get("emotion_rationale") or ""),
            conf_f,
            aj,
            camp_id,
        ),
    )
    conn.commit()


async def update_incoming_call_analysis_by_id(call_id: int, analysis: dict[str, Any]) -> bool:
    return await asyncio.to_thread(_update_incoming_call_analysis_by_id_sync, call_id, analysis)


def _update_incoming_call_analysis_by_id_sync(call_id: int, analysis: dict[str, Any]) -> bool:
    conn = _get_conn()
    row = conn.execute("SELECT id FROM incoming_calls WHERE id = ?", (int(call_id),)).fetchone()
    if not row:
        return False
    aj = json.dumps(analysis, ensure_ascii=False)
    conf = analysis.get("emotion_confidence")
    try:
        conf_f = float(conf) if conf is not None and str(conf).strip() != "" else None
    except (TypeError, ValueError):
        conf_f = None
    conn.execute(
        """
        UPDATE incoming_calls SET
            disposition = ?,
            summary = ?,
            next_steps = ?,
            emotion_label = ?,
            emotion_rationale = ?,
            emotion_confidence = ?,
            analysis_json = ?,
            updated_at = datetime('now')
        WHERE id = ?
        """,
        (
            str(analysis.get("disposition") or ""),
            str(analysis.get("summary") or ""),
            str(analysis.get("next_steps") or ""),
            str(analysis.get("emotion_label") or ""),
            str(analysis.get("emotion_rationale") or ""),
            conf_f,
            aj,
            int(call_id),
        ),
    )
    conn.commit()
    return True


async def get_incoming_call_by_id(call_id: int) -> Optional[dict]:
    return await asyncio.to_thread(_get_incoming_call_by_id_sync, call_id)


def _get_incoming_call_by_id_sync(call_id: int) -> Optional[dict]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM incoming_calls WHERE id = ?", (int(call_id),)).fetchone()
    return dict(row) if row else None


async def list_recent_incoming_calls(role: str, limit: int = 15) -> list[dict]:
    return await asyncio.to_thread(_list_recent_incoming_calls_sync, role, limit)


def _list_recent_incoming_calls_sync(role: str, limit: int = 15) -> list[dict]:
    conn = _get_conn()
    lim = max(1, min(int(limit), 5000))
    rows = conn.execute(
        """
        SELECT * FROM incoming_calls WHERE role = ?
        ORDER BY id DESC LIMIT ?
        """,
        (role, lim),
    ).fetchall()
    return [dict(r) for r in rows]


async def list_stuck_incoming_calls(max_age_minutes: int = 15, limit: int = 25) -> list[dict]:
    return await asyncio.to_thread(_list_stuck_incoming_calls_sync, max_age_minutes, limit)


def _list_stuck_incoming_calls_sync(max_age_minutes: int = 15, limit: int = 25) -> list[dict]:
    conn = _get_conn()
    lim = max(1, min(int(limit), 100))
    age = max(1, int(max_age_minutes))
    rows = conn.execute(
        """
        SELECT * FROM incoming_calls
        WHERE status IN ('connected', 'ringing')
          AND datetime(started_at) <= datetime('now', ? || ' minutes')
        ORDER BY id ASC
        LIMIT ?
        """,
        (f"-{age}", lim),
    ).fetchall()
    return [dict(r) for r in rows]


# --- Helpers ---

def coerce_extra_field(value) -> dict:
    """Return `value` as a dict, tolerating already-decoded dicts or JSON strings."""
    if isinstance(value, dict):
        return value
    if isinstance(value, (str, bytes, bytearray)):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _row_to_dict(row: sqlite3.Row) -> dict:
    out = {key: row[key] for key in row.keys()}
    # Decode the leads.extra JSON blob so callers get a normal dict and can
    # use it as ``lead["extra"]["rfq_subject"]`` without re-parsing.
    raw = out.get("extra")
    if raw is not None and isinstance(raw, str):
        if raw.strip():
            try:
                parsed = json.loads(raw)
                out["extra"] = parsed if isinstance(parsed, dict) else {}
            except Exception:
                out["extra"] = {}
        else:
            out["extra"] = {}
    return out


def migrate_from_json(data_dir: Path = None) -> dict:
    """One-time migration from JSON files to SQLite. Returns migration summary."""
    base = data_dir or Path(__file__).resolve().parent.parent / "data"
    migrated = {"roles": 0, "leads": 0, "agents": 0}

    # Migrate role states
    for role in (
        "sales_1",
    ):
        json_path = base / role / "state.json"
        if json_path.exists():
            try:
                with open(json_path) as f:
                    data = json.load(f)
                _save_role_state_sync(
                    role,
                    prompt=data.get("prompt", ""),
                    rag=data.get("rag", ""),
                    vobiz_config=data.get("vobiz", {}),
                    delay_sec=data.get("delay_sec", default_inter_call_gap_sec(role)),
                )
                migrated["roles"] += 1
            except Exception as e:
                logger.warning(f"Failed to migrate role state for {role}: {e}")

    # Migrate sandbox agents
    agents_json = Path(__file__).resolve().parent.parent / "sandbox" / "agents.json"
    if agents_json.exists():
        try:
            with open(agents_json) as f:
                agents = json.load(f)
            for agent in agents:
                agent_id = _create_agent_sync(
                    name=agent.get("name", "Unnamed"),
                    prompt=agent.get("prompt", ""),
                    voice=agent.get("voice", "Puck"),
                )
                for kf in agent.get("knowledge_files", []):
                    _add_agent_knowledge_file_sync(
                        agent_id,
                        kf.get("file_id", "unknown"),
                        kf.get("filename", "unknown"),
                        kf.get("extracted_text", ""),
                    )
                for lead in agent.get("leads", []):
                    _add_agent_lead_sync(agent_id, lead)
                migrated["agents"] += 1
        except Exception as e:
            logger.warning(f"Failed to migrate agents: {e}")

    logger.info(f"Migration complete: {migrated}")
    return migrated


# --- Callback batch processing ---

async def get_pending_callbacks(role: str, limit: int = 50) -> list[dict]:
    return await asyncio.to_thread(_get_pending_callbacks_sync, role, limit)

def _get_pending_callbacks_sync(role: str, limit: int = 50) -> list[dict]:
    """Return leads with status 'callback_scheduled' that are due for callback."""
    conn = _get_conn()
    r = (role or "sales_1").strip().lower()
    now = time.time()
    rows = conn.execute(
        """
        SELECT * FROM leads
        WHERE role = ? AND status = 'callback_scheduled'
          AND CAST(json_extract(analysis, '$.callback_reminder_epoch') AS REAL) <= ?
        ORDER BY created_at ASC LIMIT ?
        """,
        (r, now, int(limit)),
    ).fetchall()
    results = []
    for row in rows:
        d = _row_to_dict(row)
        analysis = d.get("analysis", {})
        if isinstance(analysis, str):
            try:
                analysis = json.loads(analysis)
            except Exception:
                analysis = {}
        d["from_phone"] = d.get("phone", "")
        d["matched_name"] = d.get("name", "")
        d["matched_company"] = d.get("company", "")
        results.append(d)
    return results


async def mark_callback_processed(callback_id: int, role: str) -> None:
    return await asyncio.to_thread(_mark_callback_processed_sync, callback_id, role)

def _mark_callback_processed_sync(callback_id: int, role: str) -> None:
    """Mark a callback lead as completed after processing."""
    conn = _get_conn()
    conn.execute(
        "UPDATE leads SET status = 'callback_completed', updated_at = datetime('now') WHERE id = ? AND role = ?",
        (int(callback_id), (role or "sales_1").strip().lower()),
    )
    conn.commit()
    _invalidate_state_cache()


async def mark_callback_calling(callback_id: int, role: str) -> None:
    return await asyncio.to_thread(_mark_callback_calling_sync, callback_id, role)

def _mark_callback_calling_sync(callback_id: int, role: str) -> None:
    """Mark a callback lead as actively being called to prevent duplicate attempts."""
    conn = _get_conn()
    conn.execute(
        "UPDATE leads SET status = 'callback_calling', updated_at = datetime('now') WHERE id = ? AND role = ?",
        (int(callback_id), (role or "sales_1").strip().lower()),
    )
    conn.commit()
    _invalidate_state_cache()


# --- Agent-Scheduled Callbacks ---

_SCHEDULED_CALLBACK_COLS = (
    "id, role, phone, name, lead_id, scheduled_at, status, error, "
    "disposition, summary, rating, next_action, analysis_json, "
    "outbound_phone, user_review, callback_type, follow_up_number, created_at, updated_at"
)


async def add_scheduled_callback(
    role: str,
    phone: str,
    name: str = "",
    scheduled_at: float = 0,
    lead_id: int | None = None,
    outbound_phone: str = "",
    callback_type: str = "",
    follow_up_number: int | None = None,
    analysis_json: dict | None = None,
) -> int:
    return await asyncio.to_thread(
        _add_scheduled_callback_sync,
        role,
        phone,
        name,
        scheduled_at,
        lead_id,
        outbound_phone,
        callback_type,
        follow_up_number,
        analysis_json,
    )


def _add_scheduled_callback_sync(
    role: str,
    phone: str,
    name: str = "",
    scheduled_at: float = 0,
    lead_id: int | None = None,
    outbound_phone: str = "",
    callback_type: str = "",
    follow_up_number: int | None = None,
    analysis_json: dict | None = None,
) -> int:
    conn = _get_conn()
    rid = (role or "sales_1").strip().lower()
    sched = float(scheduled_at)
    cb_type = (callback_type or "").strip()
    # Dedup: same lead + callback_type + follow_up_number when typed
    if cb_type and follow_up_number is not None and lead_id is not None:
        existing_typed = conn.execute(
            """
            SELECT id FROM scheduled_callbacks
            WHERE role = ? AND lead_id = ? AND callback_type = ? AND follow_up_number = ?
              AND status IN ('scheduled', 'queued', 'calling')
            ORDER BY id DESC LIMIT 1
            """,
            (rid, lead_id, cb_type, int(follow_up_number)),
        ).fetchone()
        if existing_typed:
            return int(existing_typed["id"])
    # Dedup: avoid double rows from live schedule_callback + post-analysis
    dup_q = """
        SELECT id FROM scheduled_callbacks
        WHERE role = ? AND phone = ? AND status IN ('scheduled', 'queued', 'calling')
          AND ABS(scheduled_at - ?) < 300
    """
    dup_params: list = [rid, phone, sched]
    if lead_id is not None:
        dup_q += " AND (lead_id IS NULL OR lead_id = ?)"
        dup_params.append(lead_id)
    dup_q += " ORDER BY id DESC LIMIT 1"
    existing = conn.execute(dup_q, dup_params).fetchone()
    if existing:
        return int(existing["id"])

    aj_str = json.dumps(analysis_json or {}, ensure_ascii=False)
    cur = conn.execute(
        "INSERT INTO scheduled_callbacks "
        "(role, phone, name, lead_id, scheduled_at, status, outbound_phone, callback_type, follow_up_number, analysis_json) "
        "VALUES (?, ?, ?, ?, ?, 'scheduled', ?, ?, ?, ?)",
        (
            (role or "sales_1").strip().lower(),
            phone,
            (name or "").strip(),
            lead_id,
            float(scheduled_at),
            (outbound_phone or "").strip(),
            cb_type,
            int(follow_up_number) if follow_up_number is not None else None,
            aj_str,
        ),
    )
    conn.commit()
    _invalidate_state_cache()
    return int(cur.lastrowid)


async def has_pending_callback_for_phone(role: str, phone: str) -> bool:
    return await asyncio.to_thread(_has_pending_callback_for_phone_sync, role, phone)


def _has_pending_callback_for_phone_sync(role: str, phone: str) -> bool:
    conn = _get_conn()
    row = conn.execute(
        "SELECT 1 FROM scheduled_callbacks WHERE role = ? AND phone = ? AND status = 'scheduled' LIMIT 1",
        (role.strip().lower(), phone.strip()),
    ).fetchone()
    return bool(row)



async def cancel_scheduled_callbacks_for_lead(lead_id: int) -> int:
    return await asyncio.to_thread(_cancel_scheduled_callbacks_for_lead_sync, lead_id)


def _cancel_scheduled_callbacks_for_lead_sync(lead_id: int) -> int:
    conn = _get_conn()
    cur = conn.execute(
        "DELETE FROM scheduled_callbacks WHERE lead_id = ? AND status = 'scheduled'",
        (lead_id,),
    )
    conn.commit()
    _invalidate_state_cache()
    return cur.rowcount


async def list_scheduled_callbacks(role: str, limit: int = 100) -> list[dict]:
    return await asyncio.to_thread(_list_scheduled_callbacks_sync, role, limit)


def _list_scheduled_callbacks_sync(role: str, limit: int = 100) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        f"SELECT {_SCHEDULED_CALLBACK_COLS} FROM scheduled_callbacks "
        "WHERE role = ? ORDER BY CASE status "
        "    WHEN 'scheduled' THEN 0 "
        "    WHEN 'queued'    THEN 1 "
        "    WHEN 'calling'   THEN 2 "
        "    ELSE 3 END, "
        "scheduled_at ASC LIMIT ?",
        ((role or "sales_1").strip().lower(), int(limit)),
    ).fetchall()
    return [dict(r) for r in rows]


async def get_due_scheduled_callbacks(role: str, now_epoch: float) -> list[dict]:
    return await asyncio.to_thread(_get_due_scheduled_callbacks_sync, role, now_epoch)


def _get_due_scheduled_callbacks_sync(role: str, now_epoch: float) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        f"SELECT {_SCHEDULED_CALLBACK_COLS} FROM scheduled_callbacks "
        "WHERE role = ? AND status = 'scheduled' AND scheduled_at <= ? "
        "ORDER BY scheduled_at ASC LIMIT 10",
        ((role or "sales_1").strip().lower(), float(now_epoch)),
    ).fetchall()
    return [dict(r) for r in rows]


async def get_queued_scheduled_callbacks(role: str) -> list[dict]:
    return await asyncio.to_thread(_get_queued_scheduled_callbacks_sync, role)


def _get_queued_scheduled_callbacks_sync(role: str) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        f"SELECT {_SCHEDULED_CALLBACK_COLS} FROM scheduled_callbacks "
        "WHERE role = ? AND status = 'queued' "
        "ORDER BY scheduled_at ASC LIMIT 10",
        ((role or "sales_1").strip().lower(),),
    ).fetchall()
    return [dict(r) for r in rows]


async def get_next_immediate_callback(
    role: str,
    now_epoch: float,
    modulo: int = None,
    remainder: int = None,
) -> dict | None:
    return await asyncio.to_thread(_get_next_immediate_callback_sync, role, now_epoch, modulo, remainder)


def _get_next_immediate_callback_sync(
    role: str,
    now_epoch: float,
    modulo: int = None,
    remainder: int = None,
) -> dict | None:
    """Return the most urgent callback: either a due scheduled one or a queued one."""
    conn = _get_conn()
    query = f"SELECT {_SCHEDULED_CALLBACK_COLS} FROM scheduled_callbacks WHERE role = ?"
    params = [(role or "sales_1").strip().lower()]
    if modulo is not None and remainder is not None:
        query += " AND IFNULL(lead_id, id) % ? = ?"
        params.extend([modulo, remainder])
    query += " AND ((status = 'scheduled' AND scheduled_at <= ?) OR status = 'queued') ORDER BY scheduled_at ASC LIMIT 1"
    params.append(float(now_epoch))
    row = conn.execute(query, params).fetchone()
    return dict(row) if row else None


async def update_scheduled_callback_status(
    callback_id: int, status: str, error: str | None = None
) -> bool:
    return await asyncio.to_thread(
        _update_scheduled_callback_status_sync, callback_id, status, error
    )


def _update_scheduled_callback_status_sync(
    callback_id: int, status: str, error: str | None = None
) -> bool:
    conn = _get_conn()
    if error is not None:
        cur = conn.execute(
            "UPDATE scheduled_callbacks SET status = ?, error = ?, updated_at = datetime('now') WHERE id = ?",
            (status, error[:500], int(callback_id)),
        )
    else:
        cur = conn.execute(
            "UPDATE scheduled_callbacks SET status = ?, updated_at = datetime('now') WHERE id = ?",
            (status, int(callback_id)),
        )
    conn.commit()
    _invalidate_state_cache()
    return cur.rowcount > 0


async def cancel_scheduled_callback(callback_id: int) -> bool:
    return await asyncio.to_thread(_cancel_scheduled_callback_sync, callback_id)


def _cancel_scheduled_callback_sync(callback_id: int) -> bool:
    """Cancel a callback that hasn't started yet."""
    conn = _get_conn()
    cur = conn.execute(
        "UPDATE scheduled_callbacks SET status = 'cancelled', updated_at = datetime('now') "
        "WHERE id = ? AND status IN ('scheduled', 'queued')",
        (int(callback_id),),
    )
    conn.commit()
    _invalidate_state_cache()
    return cur.rowcount > 0


async def get_scheduled_callback(callback_id: int) -> dict | None:
    return await asyncio.to_thread(_get_scheduled_callback_sync, callback_id)


def _get_scheduled_callback_sync(callback_id: int) -> dict | None:
    conn = _get_conn()
    row = conn.execute(
        f"SELECT {_SCHEDULED_CALLBACK_COLS} FROM scheduled_callbacks WHERE id = ?",
        (int(callback_id),),
    ).fetchone()
    return dict(row) if row else None


async def update_scheduled_callback_review(callback_id: int, review: str) -> bool:
    """Mark a scheduled callback as interested or not_interested (dashboard manual review)."""
    return await asyncio.to_thread(_update_scheduled_callback_review_sync, callback_id, review)


def _update_scheduled_callback_review_sync(callback_id: int, review: str) -> bool:
    review_norm = (review or "").strip().lower()
    if review_norm not in ("interested", "not_interested", ""):
        return False
    conn = _get_conn()
    cur = conn.execute(
        "UPDATE scheduled_callbacks SET user_review = ?, updated_at = datetime('now') WHERE id = ?",
        (review_norm, int(callback_id)),
    )
    conn.commit()
    return cur.rowcount > 0


# ── WhatsApp dedup ────────────────────────────────────────────────

async def mark_whatsapp_sent(lead_id: int) -> None:
    return await asyncio.to_thread(_mark_whatsapp_sent_sync, lead_id)


def _mark_whatsapp_sent_sync(lead_id: int) -> None:
    conn = _get_conn()
    conn.execute(
        "UPDATE leads SET whatsapp_sent = 1, whatsapp_sent_at = ?, updated_at = datetime('now') WHERE id = ?",
        (time.time(), int(lead_id)),
    )
    conn.commit()


# ── Failed Call Retry and WhatsApp Reminder Helpers ──────────────────

async def update_lead_retry_state(lead_id: int, status: str, extra: dict, analysis: dict) -> None:
    await asyncio.to_thread(_update_lead_retry_state_sync, lead_id, status, extra, analysis)
    try:
        from core.events import get_event_bus
        role = await get_lead_role(lead_id)
        if role:
            await get_event_bus().publish("lead_updated", role=role, lead_id=lead_id)
    except Exception:
        pass


def _update_lead_retry_state_sync(lead_id: int, status: str, extra: dict, analysis: dict) -> None:
    conn = _get_conn()

    # Capture old status for DashboardState notification
    _old_status = "dialing"
    try:
        _row = conn.execute("SELECT role, status FROM leads WHERE id = ?", (lead_id,)).fetchone()
        if _row:
            _old_status = str(_row["status"] or "dialing").strip().lower()
    except Exception:
        pass

    conn.execute(
        "UPDATE leads SET status = ?, extra = ?, analysis = ?, updated_at = datetime('now') WHERE id = ?",
        (status, json.dumps(extra), json.dumps(analysis), int(lead_id))
    )
    
    # Cancel pending scheduled retries if resolved
    s_lower = (status or "").lower()
    if s_lower in ("completed", "not_interested", "callback_completed", "callback_scheduled", "site_visit", "site_visited", "interested", "dnc"):
        conn.execute(
            "DELETE FROM scheduled_callbacks WHERE lead_id = ? AND status = 'scheduled'",
            (lead_id,)
        )
        
    # Also persist duration to the dedicated column so CSV exports can read it directly
    dur = analysis.get("duration")
    if dur is not None:
        try:
            conn.execute(
                "UPDATE leads SET duration_sec = ? WHERE id = ?",
                (round(float(dur), 1), int(lead_id))
            )
        except Exception:
            pass
    conn.commit()
    _invalidate_state_cache()

    # Notify materialized dashboard state
    try:
        from core.dashboard_state import notify_lead_updated
        _role = "sales_1"
        try:
            _rrow = conn.execute("SELECT role FROM leads WHERE id = ?", (lead_id,)).fetchone()
            if _rrow:
                _role = str(_rrow["role"])
        except Exception:
            pass
        notify_lead_updated(
            role=_role, lead_id=lead_id,
            old_status=_old_status, new_status=str(status or "").strip().lower(),
            analysis_raw=analysis,
        )
    except Exception:
        pass



async def get_due_whatsapp_reminders(before_epoch: float) -> list[dict]:
    return await asyncio.to_thread(_get_due_whatsapp_reminders_sync, before_epoch)


def _get_due_whatsapp_reminders_sync(before_epoch: float) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        """
        SELECT id, role, name, phone, status, extra, analysis FROM leads
        WHERE whatsapp_sent = 1
          AND whatsapp_reminder_sent = 0
          AND whatsapp_sent_at IS NOT NULL
          AND whatsapp_sent_at <= ?
          AND (status != 'not_interested' OR role IN ('sales_1'))
        """,
        (float(before_epoch),),
    ).fetchall()
    return [dict(r) for r in rows]


async def mark_whatsapp_reminder_sent(lead_id: int) -> None:
    return await asyncio.to_thread(_mark_whatsapp_reminder_sent_sync, lead_id)


def _mark_whatsapp_reminder_sent_sync(lead_id: int) -> None:
    conn = _get_conn()
    conn.execute(
        "UPDATE leads SET whatsapp_reminder_sent = 1, updated_at = datetime('now') WHERE id = ?",
        (int(lead_id),),
    )
    conn.commit()
    _invalidate_state_cache()


async def get_lead_whatsapp_sent(lead_id: int) -> bool:
    return await asyncio.to_thread(_get_lead_whatsapp_sent_sync, lead_id)


def _get_lead_whatsapp_sent_sync(lead_id: int) -> bool:
    conn = _get_conn()
    row = conn.execute(
        "SELECT whatsapp_sent FROM leads WHERE id = ?",
        (int(lead_id),),
    ).fetchone()
    return bool(row and row[0])


# ── Email dedup ───────────────────────────────────────────────────

async def mark_email_sent(lead_id: int) -> None:
    return await asyncio.to_thread(_mark_email_sent_sync, lead_id)


def _mark_email_sent_sync(lead_id: int) -> None:
    conn = _get_conn()
    conn.execute(
        "UPDATE leads SET email_sent = 1, email_sent_at = ?, updated_at = datetime('now') WHERE id = ?",
        (time.time(), int(lead_id)),
    )
    conn.commit()


async def update_lead_email_sent_in_db(lead_id: int, email: str) -> None:
    return await asyncio.to_thread(_update_lead_email_sent_in_db_sync, lead_id, email)


def _update_lead_email_sent_in_db_sync(lead_id: int, email: str) -> None:
    conn = _get_conn()
    row = conn.execute("SELECT extra FROM leads WHERE id = ?", (int(lead_id),)).fetchone()
    extra_data = {}
    if row and row[0]:
        try:
            import json
            extra_data = json.loads(row[0])
        except Exception:
            pass
    extra_data["_email_sent"] = True
    extra_data["email"] = email
    
    import json
    conn.execute(
        "UPDATE leads SET email = ?, email_sent = 1, email_sent_at = ?, extra = ?, updated_at = datetime('now') WHERE id = ?",
        (email, time.time(), json.dumps(extra_data), int(lead_id)),
    )
    conn.commit()


async def get_lead_email_sent(lead_id: int) -> bool:
    return await asyncio.to_thread(_get_lead_email_sent_sync, lead_id)


def _get_lead_email_sent_sync(lead_id: int) -> bool:
    conn = _get_conn()
    row = conn.execute(
        "SELECT email_sent FROM leads WHERE id = ?",
        (int(lead_id),),
    ).fetchone()
    return bool(row and row[0])


# ── Scheduled callback analysis ───────────────────────────────────

async def update_scheduled_callback_analysis(
    callback_id: int,
    disposition: str = "",
    summary: str = "",
    rating: float | None = None,
    next_action: dict | None = None,
    analysis_json: dict | None = None,
) -> None:
    return await asyncio.to_thread(
        _update_scheduled_callback_analysis_sync,
        callback_id, disposition, summary, rating, next_action, analysis_json,
    )


def _update_scheduled_callback_analysis_sync(
    callback_id: int,
    disposition: str = "",
    summary: str = "",
    rating: float | None = None,
    next_action: dict | None = None,
    analysis_json: dict | None = None,
) -> None:
    import json as _json
    conn = _get_conn()
    conn.execute(
        "UPDATE scheduled_callbacks SET "
        "disposition = ?, summary = ?, rating = ?, "
        "next_action = ?, analysis_json = ?, updated_at = datetime('now') "
        "WHERE id = ?",
        (
            (disposition or "")[:500],
            (summary or "")[:2000],
            rating,
            _json.dumps(next_action or {}),
            _json.dumps(analysis_json or {}),
            int(callback_id),
        ),
    )
    conn.commit()
    _invalidate_state_cache()


# ── Call Attempt History CRUD ─────────────────────────────────────

_CA_COLS = (
    "id, lead_id, role, attempt_number, log_id, status, disposition, summary, rating, "
    "duration_sec, callback_scheduled_at, error, created_at, call_category, follow_up_number"
)

_CA_COLS_LIST = _CA_COLS


async def add_call_attempt(
    lead_id: int,
    role: str,
    attempt_number: int = 1,
    log_id: str = "",
    status: str = "completed",
    disposition: str = "",
    summary: str = "",
    rating: float | None = None,
    duration_sec: float | None = None,
    callback_scheduled_at: float | None = None,
    error: str = "",
    call_category: str = "initial",
    follow_up_number: int | None = None,
) -> int:
    return await asyncio.to_thread(
        _add_call_attempt_sync,
        lead_id, role, attempt_number, log_id, status,
        disposition, summary, rating, duration_sec, callback_scheduled_at, error,
        call_category, follow_up_number,
    )


def _add_call_attempt_sync(
    lead_id: int,
    role: str,
    attempt_number: int = 1,
    log_id: str = "",
    status: str = "completed",
    disposition: str = "",
    summary: str = "",
    rating: float | None = None,
    duration_sec: float | None = None,
    callback_scheduled_at: float | None = None,
    error: str = "",
    call_category: str = "initial",
    follow_up_number: int | None = None,
) -> int:
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO call_attempts "
        "(lead_id, role, attempt_number, log_id, status, disposition, summary, rating, duration_sec, "
        "callback_scheduled_at, error, call_category, follow_up_number) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            int(lead_id),
            role,
            int(attempt_number),
            (log_id or "")[:200],
            (status or "completed")[:50],
            (disposition or "")[:200],
            (summary or "")[:3000],
            rating,
            duration_sec,
            callback_scheduled_at,
            (error or "")[:500],
            (call_category or "initial")[:30],
            int(follow_up_number) if follow_up_number is not None else None,
        ),
    )
    if log_id and lead_id:
        conn.execute(
            "UPDATE leads SET _log_id = ? WHERE id = ? AND COALESCE(TRIM(_log_id), '') = ''",
            ((log_id or "")[:200], int(lead_id)),
        )
    conn.commit()
    _invalidate_state_cache()
    return cur.lastrowid


async def get_call_attempts(lead_id: int) -> list[dict]:
    return await asyncio.to_thread(_get_call_attempts_sync, lead_id)


def _get_call_attempts_sync(lead_id: int) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        f"SELECT {_CA_COLS_LIST} FROM call_attempts WHERE lead_id = ? ORDER BY attempt_number ASC, id ASC",
        (int(lead_id),),
    ).fetchall()
    return [dict(r) for r in rows]


def pick_best_call_attempt(attempts: list[dict]) -> dict | None:
    """Score attempts; return highest-scoring row for dashboard main display."""
    if not attempts:
        return None

    def _score(a: dict) -> float:
        s = 0.0
        log_id = (a.get("log_id") or "").strip()
        if log_id:
            try:
                from services.call_recording import recording_duration_sec, resolve_dashboard_recording_path

                dur = recording_duration_sec(log_id)
                if dur is not None and dur >= 20:
                    s += 50
                rp = resolve_dashboard_recording_path(log_id)
                if rp and rp.is_file():
                    s += 10
            except Exception:
                pass
        status = str(a.get("status") or "").lower()
        if status in ("completed", "site_visit", "callback_scheduled"):
            s += 40
        disp = str(a.get("disposition") or "").lower()
        if any(x in disp for x in ("interested", "site visit", "callback", "call later")):
            s += 20
        try:
            rating = float(a.get("rating") or 0)
            s += rating * 5
        except (TypeError, ValueError):
            pass
        try:
            dur_sec = float(a.get("duration_sec") or 0)
            s += min(dur_sec, 120) / 4
        except (TypeError, ValueError):
            pass
        if status in ("failed", "busy", "no answer") or disp in ("no answer", "voicemail", "busy"):
            s -= 30
        return s

    return max(attempts, key=_score)


async def sync_lead_best_attempt(lead_id: int) -> dict | None:
    """Recompute best attempt and persist best_attempt_log_id on lead extra."""
    attempts = await get_call_attempts(lead_id)
    best = pick_best_call_attempt(attempts)
    if not best:
        return None
    conn = _get_conn()
    row = conn.execute("SELECT extra, analysis FROM leads WHERE id = ?", (int(lead_id),)).fetchone()
    if not row:
        return best
    extra_raw = row["extra"] or "{}"
    try:
        extra = json.loads(extra_raw) if isinstance(extra_raw, str) else (extra_raw or {})
    except json.JSONDecodeError:
        extra = {}
    if not isinstance(extra, dict):
        extra = {}
    extra["best_attempt_log_id"] = (best.get("log_id") or "")[:200]
    extra["best_attempt_number"] = int(best.get("attempt_number") or 1)
    log_id = (best.get("log_id") or "").strip()
    conn.execute(
        "UPDATE leads SET extra = ?, _log_id = COALESCE(NULLIF(?, ''), _log_id) WHERE id = ?",
        (json.dumps(extra, ensure_ascii=False), log_id[:200], int(lead_id)),
    )
    conn.commit()
    _invalidate_state_cache()
    return best


# ── Virtual Meet CRUD ───────────────────────────────────────────

_VM_COLS = "id, lead_id, role, meet_date, meet_time, notes, status, rescheduled_from_id, created_at, updated_at"


async def add_virtual_meet(
    lead_id: int,
    role: str,
    meet_date: str,
    meet_time: str,
    notes: str = "",
) -> int:
    return await asyncio.to_thread(_add_virtual_meet_sync, lead_id, role, meet_date, meet_time, notes)


def _add_virtual_meet_sync(
    lead_id: int,
    role: str,
    meet_date: str,
    meet_time: str,
    notes: str = "",
) -> int:
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO virtual_meets (lead_id, role, meet_date, meet_time, notes, status) "
        "VALUES (?, ?, ?, ?, ?, 'scheduled')",
        (int(lead_id), (role or "").strip().lower(), meet_date.strip(), meet_time.strip(), (notes or "").strip()),
    )
    conn.commit()
    return int(cur.lastrowid)


async def get_virtual_meet_for_lead(lead_id: int) -> dict | None:
    return await asyncio.to_thread(_get_virtual_meet_for_lead_sync, lead_id)


def _get_virtual_meet_for_lead_sync(lead_id: int) -> dict | None:
    conn = _get_conn()
    row = conn.execute(
        f"SELECT {_VM_COLS} FROM virtual_meets WHERE lead_id = ? ORDER BY id DESC LIMIT 1",
        (int(lead_id),),
    ).fetchone()
    return dict(row) if row else None


async def reschedule_virtual_meet(
    meet_id: int,
    new_date: str,
    new_time: str,
    new_notes: str = "",
) -> bool:
    return await asyncio.to_thread(_reschedule_virtual_meet_sync, meet_id, new_date, new_time, new_notes)


def _reschedule_virtual_meet_sync(
    meet_id: int,
    new_date: str,
    new_time: str,
    new_notes: str = "",
) -> bool:
    """Create a new virtual_meet row with status='scheduled' and link back to the old one as rescheduled_from_id."""
    conn = _get_conn()
    old = conn.execute("SELECT * FROM virtual_meets WHERE id = ?", (int(meet_id),)).fetchone()
    if not old:
        return False
    conn.execute(
        "UPDATE virtual_meets SET status = 'rescheduled', updated_at = datetime('now') WHERE id = ?",
        (int(meet_id),),
    )
    cur = conn.execute(
        "INSERT INTO virtual_meets (lead_id, role, meet_date, meet_time, notes, status, rescheduled_from_id) "
        "VALUES (?, ?, ?, ?, ?, 'scheduled', ?)",
        (old["lead_id"], old["role"], new_date.strip(), new_time.strip(), (new_notes or "").strip(), int(meet_id)),
    )
    conn.commit()
    return cur.rowcount > 0


async def cancel_virtual_meet(meet_id: int) -> bool:
    return await asyncio.to_thread(_cancel_virtual_meet_sync, meet_id)


def _cancel_virtual_meet_sync(meet_id: int) -> bool:
    conn = _get_conn()
    cur = conn.execute(
        "UPDATE virtual_meets SET status = 'cancelled', updated_at = datetime('now') WHERE id = ?",
        (int(meet_id),),
    )
    conn.commit()
    return cur.rowcount > 0


async def is_duplicate_lead(role: str, phone: str, lead_id: int) -> bool:
    return await asyncio.to_thread(_is_duplicate_lead_sync, role, phone, lead_id)


def _is_duplicate_lead_sync(role: str, phone: str, lead_id: int) -> bool:
    digits = "".join(c for c in str(phone) if c.isdigit())
    if len(digits) < 10:
        return False
    tail = digits[-10:]
    conn = _get_conn()
    
    # Check if any other lead with the same last 10 digits in the same role has been processed or is active
    rows = conn.execute(
        """
        SELECT id, status FROM leads 
        WHERE role = ? 
          AND phone LIKE ? 
          AND id != ?
        """,
        ((role or "").strip().lower(), f"%{tail}", int(lead_id))
    ).fetchall()
    
    for r in rows:
        other_id = r["id"]
        other_status = r["status"]
        if other_status in ('dialing', 'completed', 'failed', 'not_interested', 'callback_scheduled', 'site_visit', 'callback_completed'):
            return True
        if other_status == 'pending' and other_id < lead_id:
            return True
            
    return False


async def get_daily_call_count_for_phone(phone_number: str) -> int:
    return await asyncio.to_thread(_get_daily_call_count_for_phone_sync, phone_number)


def _get_daily_call_count_for_phone_sync(phone_number: str) -> int:
    import time
    from datetime import datetime
    import zoneinfo
    
    tz = zoneinfo.ZoneInfo("Asia/Kolkata")
    now_ist = datetime.now(tz)
    midnight_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    midnight_epoch = midnight_ist.timestamp()
    
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM leads WHERE outbound_phone = ? AND start_time >= ?",
        (phone_number, midnight_epoch)
    )
    count = cur.fetchone()[0]
    return count


async def get_daily_call_count_for_source(role: str, source_name: str) -> int:
    return await asyncio.to_thread(_get_daily_call_count_for_source_sync, role, source_name)


def _get_daily_call_count_for_source_sync(role: str, source_name: str) -> int:
    from datetime import datetime
    import zoneinfo

    tz = zoneinfo.ZoneInfo("Asia/Kolkata")
    now_ist = datetime.now(tz)
    midnight_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    midnight_epoch = midnight_ist.timestamp()

    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        """SELECT COUNT(*) FROM leads
           WHERE role = ?
             AND json_extract(extra, '$.upload_source') = ?
             AND start_time >= ?""",
        (role, source_name, midnight_epoch),
    )
    return cur.fetchone()[0]


async def get_campaign_sources(role: str, paused_sources: list) -> list[dict]:
    return await asyncio.to_thread(_get_campaign_sources_sync, role, paused_sources)


def _get_campaign_sources_sync(role: str, paused_sources: list) -> list[dict]:
    from core.state import normalize_console_role
    role = normalize_console_role(role)
    conn = _get_conn()
    query = """
        SELECT 
            json_extract(extra, '$.upload_source') as src_name,
            sandbox,
            COUNT(*) as total,
            SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending,
            SUM(CASE WHEN status != 'pending' THEN 1 ELSE 0 END) as called,
            SUM(CASE WHEN json_extract(analysis, '$.disposition') LIKE '%Interested%' OR CAST(json_extract(analysis, '$.outcome_from_transcript') AS INTEGER) = 1 THEN 1 ELSE 0 END) as interested,
            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed
        FROM leads
        WHERE role = ?
          AND json_extract(extra, '$.upload_source') IS NOT NULL
          AND json_extract(extra, '$.upload_source') != ''
        GROUP BY json_extract(extra, '$.upload_source'), sandbox
    """
    rows = conn.execute(query, [role]).fetchall()
    # Merge by source name, keeping sandbox as the dominant sandbox for that source
    merged: dict[str, dict] = {}
    for r in rows:
        src_name = r[0]
        sandbox = r[1]
        if src_name not in merged:
            merged[src_name] = {
                "name": src_name,
                "sandbox": sandbox,
                "total": 0, "pending": 0, "called": 0, "interested": 0, "failed": 0,
                "paused": src_name in paused_sources
            }
        m = merged[src_name]
        m["total"] += r[2]
        m["pending"] += r[3]
        m["called"] += r[4]
        m["interested"] += r[5]
        m["failed"] += r[6]
        # Keep the most common sandbox
        if r[2] > (m.get("_max_count", 0)):
            m["sandbox"] = sandbox
            m["_max_count"] = r[2]
    result = list(merged.values())
    for m in result:
        m.pop("_max_count", None)
    result.sort(key=lambda x: x["name"])
    return result


async def get_recent_call_outcomes_for_phone(phone_number: str, since_epoch: float) -> list[dict]:
    return await asyncio.to_thread(_get_recent_call_outcomes_for_phone_sync, phone_number, since_epoch)


def _get_recent_call_outcomes_for_phone_sync(phone_number: str, since_epoch: float) -> list[dict]:
    from core.phone_norm import norm_phone_str
    norm = norm_phone_str(phone_number)
    
    conn = _get_conn()
    query = """
        SELECT status, error, analysis, extra 
        FROM leads 
        WHERE outbound_phone = ? AND start_time >= ?
        ORDER BY start_time DESC
    """
    rows = conn.execute(query, [norm, since_epoch]).fetchall()
    return [_row_to_dict(r) for r in rows]


async def get_recent_call_outcomes_for_role(role: str, since_epoch: float) -> list[dict]:
    return await asyncio.to_thread(_get_recent_call_outcomes_for_role_sync, role, since_epoch)


def _get_recent_call_outcomes_for_role_sync(role: str, since_epoch: float) -> list[dict]:
    from core.state import normalize_console_role
    r = normalize_console_role(role)
    
    conn = _get_conn()
    query = """
        SELECT status, error, analysis, extra 
        FROM leads 
        WHERE role = ? AND start_time >= ?
        ORDER BY start_time DESC
    """
    rows = conn.execute(query, [r, since_epoch]).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_paused_sources_sync(role: str) -> list[str]:
    """Return paused sources for role — reads from in-memory cache first."""
    from core.state import normalize_console_role
    role = normalize_console_role(role)
    with _PAUSED_SOURCES_LOCK:
        if role in _PAUSED_SOURCES:
            return list(_PAUSED_SOURCES[role])
    # Not in memory yet — load from SQLite once and populate cache
    conn = _get_conn()
    key = f"paused_sources:{role}"
    row = conn.execute("SELECT value FROM app_meta WHERE key = ?", (key,)).fetchone()
    result: list[str] = []
    if row and row[0]:
        try:
            val = json.loads(row[0])
            if isinstance(val, list):
                result = [str(s) for s in val]
        except Exception:
            result = [s.strip() for s in row[0].split(",") if s.strip()]
    with _PAUSED_SOURCES_LOCK:
        _PAUSED_SOURCES[role] = result
    return list(result)


async def get_paused_sources(role: str) -> list[str]:
    """Async wrapper — reads from in-memory cache (no thread dispatch needed)."""
    return get_paused_sources_sync(role)


def set_paused_sources_sync(role: str, sources: list[str]) -> None:
    """Update paused sources in memory and persist to SQLite."""
    from core.state import normalize_console_role
    role = normalize_console_role(role)
    # 1. Update in-memory cache immediately so all threads see it right away
    with _PAUSED_SOURCES_LOCK:
        _PAUSED_SOURCES[role] = list(sources)
    # 2. Persist to SQLite for restart durability
    conn = _get_conn()
    key = f"paused_sources:{role}"
    val_str = json.dumps(sources)
    conn.execute(
        "INSERT OR REPLACE INTO app_meta(key, value) VALUES (?, ?)",
        (key, val_str)
    )
    conn.commit()
    # 3. Force WAL checkpoint so other connections see it
    try:
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    except Exception:
        pass


async def set_paused_sources(role: str, sources: list[str]) -> None:
    """Async wrapper — updates in-memory cache synchronously then persists."""
    set_paused_sources_sync(role, sources)
    try:
        from core.events import get_event_bus
        await get_event_bus().publish("lead_updated", role=role, lead_id=None)
    except Exception:
        pass


async def claim_next_immediate_callback(
    role: str, now_epoch: float, outbound_phone: str = ""
) -> dict | None:
    return await asyncio.to_thread(
        _claim_next_immediate_callback_sync, role, now_epoch, outbound_phone
    )


def _claim_next_immediate_callback_sync(
    role: str, now_epoch: float, outbound_phone: str = ""
) -> dict | None:
    """Atomically fetch and claim the next due scheduled callback for a role."""
    from core.phone_norm import norm_phone_str

    conn = _get_conn()
    norm_out = norm_phone_str(outbound_phone)
    with conn:
        query = f"""
            SELECT {_SCHEDULED_CALLBACK_COLS} FROM scheduled_callbacks
            WHERE role = ? AND ((status = 'scheduled' AND scheduled_at <= ?) OR status = 'queued')
            ORDER BY scheduled_at ASC
        """
        rows = conn.execute(
            query,
            ((role or "").strip().lower(), float(now_epoch)),
        ).fetchall()
        row = None
        for candidate in rows:
            cb_out = norm_phone_str(str(candidate["outbound_phone"] or ""))
            # Empty outbound_phone = any line on this role may dial the callback
            if not cb_out or not norm_out or cb_out == norm_out:
                row = candidate
                break
        if not row:
            return None
        cb = dict(row)
        conn.execute(
            "UPDATE scheduled_callbacks SET status = 'calling', updated_at = datetime('now') WHERE id = ?",
            (cb["id"],),
        )
        return cb


# ── Lead Memory helpers (rolling summary / preferences for conversation continuity) ──

async def get_lead_memory(lead_id: int) -> dict | None:
    """Return the rolling memory dict for a lead: {facts_json, summary, last_interaction_at}."""
    return await asyncio.to_thread(_get_lead_memory_sync, lead_id)


def _get_lead_memory_sync(lead_id: int) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM lead_memory WHERE lead_id = ?", (lead_id,)).fetchone()
    if not row:
        return None
    return dict(row)


async def upsert_lead_memory(lead_id: int, *, facts: dict = None, summary: str = None) -> bool:
    """Insert or update the rolling lead memory. Returns True on success."""
    return await asyncio.to_thread(_upsert_lead_memory_sync, lead_id, facts=facts, summary=summary)


def _upsert_lead_memory_sync(lead_id: int, *, facts: dict = None, summary: str = None) -> bool:
    import json
    conn = _get_conn()
    now = time.time()
    existing = conn.execute("SELECT facts_json, summary FROM lead_memory WHERE lead_id = ?", (lead_id,)).fetchone()
    if existing:
        curr_facts = json.loads(existing["facts_json"]) if existing["facts_json"] else {}
        if facts:
            curr_facts.update(facts)
        new_summary = summary if summary is not None else existing["summary"]
        conn.execute(
            "UPDATE lead_memory SET facts_json=?, summary=?, last_interaction_at=?, updated_at=datetime('now') WHERE lead_id=?",
            (json.dumps(curr_facts, ensure_ascii=False), new_summary, now, lead_id),
        )
    else:
        conn.execute(
            "INSERT INTO lead_memory (lead_id, facts_json, summary, last_interaction_at) VALUES (?, ?, ?, ?)",
            (lead_id, json.dumps(facts or {}, ensure_ascii=False), summary or "", now),
        )
    conn.commit()
    return True


async def log_whatsapp_message(lead_id: int, phone: str, message_type: str, direction: str = "outbound", content: str = "") -> int:
    """Log a WhatsApp message and return its ID."""
    return await asyncio.to_thread(_log_whatsapp_message_sync, lead_id, phone, message_type, direction, content)


def _log_whatsapp_message_sync(lead_id: int, phone: str, message_type: str, direction: str = "outbound", content: str = "") -> int:
    conn = _get_conn()
    now = time.time()
    cur = conn.execute(
        "INSERT INTO whatsapp_messages (lead_id, phone, message_type, direction, content, sent_at, status) VALUES (?, ?, ?, ?, ?, ?, 'sent')",
        (lead_id, phone, message_type, direction, content, now),
    )
    conn.commit()
    return cur.lastrowid


async def get_last_whatsapp_for_lead(lead_id: int, message_type: str = None) -> dict | None:
    """Get the most recent WhatsApp message for a lead."""
    return await asyncio.to_thread(_get_last_whatsapp_for_lead_sync, lead_id, message_type)


def _get_last_whatsapp_for_lead_sync(lead_id: int, message_type: str = None) -> dict | None:
    conn = _get_conn()
    if message_type:
        row = conn.execute(
            "SELECT * FROM whatsapp_messages WHERE lead_id=? AND message_type=? ORDER BY id DESC LIMIT 1",
            (lead_id, message_type),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM whatsapp_messages WHERE lead_id=? ORDER BY id DESC LIMIT 1",
            (lead_id,),
        ).fetchone()
    return dict(row) if row else None
