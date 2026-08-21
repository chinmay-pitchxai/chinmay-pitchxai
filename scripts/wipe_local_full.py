"""Wipe all local leads and campaign history for a fresh start."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND))

from core import kv_cache
from core.state import _CAMPAIGN_DATA
from core.storage import (
    _get_conn,
    init_db,
    set_campaign_globally_paused,
    set_campaign_want_running,
    set_paused_sources,
    wipe_leads,
)

ROLES = ("sales_1",)
DATA_DIR = _BACKEND / "data"

WIPE_TABLES = (
    "workflow_jobs",
    "call_attempts",
    "scheduled_callbacks",
    "vobiz_call_map",
    "whatsapp_messages",
    "feedback_records",
    "site_visits",
    "lead_memory",
    "manual_calls",
    "camp_sessions",
    "campaign_contacts",
    "incoming_calls",
    "conversation_messages",
    "virtual_meets",
    "schedules",
    "agent_leads",
    "cases",
    "do_not_contact",
    "dnc_list",
)

CLEAR_DIRS = (
    "call_recordings",
    "conversation_logs",
    "digital_leads",
    "recordings",
    "source_files",
    "transcripts",
    "Technopolis_Call_Recordings",
)


def _count(table: str) -> int:
    conn = _get_conn()
    try:
        row = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
        return int(row["c"] or 0)
    except Exception:
        return -1


async def main() -> None:
    init_db(str(DATA_DIR))
    conn = _get_conn()

    print("BEFORE:")
    for table in ("leads", *WIPE_TABLES):
        print(f"  {table}: {_count(table)}")

    await set_campaign_globally_paused(True)
    for role in ROLES:
        await set_campaign_want_running(role, False)
        await set_paused_sources(role, [])
        await wipe_leads(role)
        kv_cache.invalidate_role(role)
    kv_cache.invalidate_all()

    try:
        from core.dashboard_state import get_dashboard_state, invalidate_all as dash_invalidate_all
        dash_invalidate_all()
        for role in ROLES:
            get_dashboard_state(role).load_from_db()
    except Exception as exc:
        print(f"dashboard_state reload warning: {exc}")

    # Delete dependent rows before leads, in one explicit transaction.  Some
    # installations do not yet have every optional table, so tolerate only
    # the missing-table case while keeping all other database errors fatal.
    for table in WIPE_TABLES:
        try:
            conn.execute(f'DELETE FROM "{table}"')
        except Exception as exc:
            if "no such table" not in str(exc).lower():
                raise
    conn.execute("DELETE FROM leads")
    conn.execute("DELETE FROM campaign_states")
    conn.execute(
        "DELETE FROM app_meta WHERE key LIKE 'paused_sources:%' "
        "OR key LIKE 'campaign_want_running%'"
    )
    conn.commit()
    _CAMPAIGN_DATA.clear()

    greetings = DATA_DIR / "greetings"
    removed_nv = 0
    if greetings.is_dir():
        for path in greetings.glob("name_verify_*"):
            path.unlink(missing_ok=True)
            removed_nv += 1

    removed_files = 0
    for dirname in CLEAR_DIRS:
        root = DATA_DIR / dirname
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink(missing_ok=True)
                removed_files += 1
            elif path.is_dir():
                try:
                    path.rmdir()
                except OSError:
                    pass

    print("AFTER:")
    for table in ("leads", *WIPE_TABLES):
        print(f"  {table}: {_count(table)}")
    for role in ROLES:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM leads WHERE role = ?", (role,)
        ).fetchone()
        print(f"  leads[{role}]: {int(row['c'] or 0)}")
    print(f"removed name_verify caches: {removed_nv}")
    print(f"removed operational files: {removed_files}")
    print("DONE: local leads + campaign history wiped clean")


if __name__ == "__main__":
    asyncio.run(main())
