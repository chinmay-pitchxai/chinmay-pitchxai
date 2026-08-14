"""VPS hygiene routines executed by Super Boss."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from loguru import logger


def cleanup_temp_deploy_artifacts() -> dict[str, Any]:
    """Remove leftover deploy tarballs and extract temp files."""
    removed: list[str] = []
    candidates = [
        Path("/tmp/technopolis-deploy.tar.gz"),
        Path("/tmp/technopolis-deploy"),
    ]
    for p in candidates:
        try:
            if p.is_file():
                p.unlink()
                removed.append(str(p))
            elif p.is_dir():
                import shutil
                shutil.rmtree(p, ignore_errors=True)
                removed.append(str(p))
        except Exception as e:
            logger.debug("Boss cleanup: could not remove {}: {}", p, e)
    return {"removed": removed, "count": len(removed)}


def cleanup_pycache_under_app(app_root: str | Path) -> dict[str, Any]:
    """Prune __pycache__ directories under the app tree (safe on VPS)."""
    root = Path(app_root)
    removed = 0
    if not root.exists():
        return {"removed_dirs": 0}
    for cache_dir in root.rglob("__pycache__"):
        try:
            import shutil
            shutil.rmtree(cache_dir, ignore_errors=True)
            removed += 1
        except Exception:
            pass
    return {"removed_dirs": removed}


async def cleanup_orphaned_dialing() -> dict[str, Any]:
    """Release dialing leads when no active calls are running."""
    from core.state import total_active_vobiz_calls
    from core.worker import release_orphaned_dialing_leads

    if total_active_vobiz_calls() > 0:
        return {"skipped": True, "reason": "active_calls"}
    released = 0
    for role in ("sales_1",):
        try:
            n = await release_orphaned_dialing_leads(
                role,
                to_status="pending",
                error="Boss cleanup: released orphaned dialing state.",
            )
            released += int(n or 0)
        except Exception as e:
            logger.warning("Boss cleanup orphaned dialing role={}: {}", role, e)
    return {"released": released}


async def dedupe_pending_callbacks() -> dict[str, Any]:
    """Remove duplicate scheduled callbacks for the same lead (keep earliest)."""
    from core.storage import _get_conn

    conn = _get_conn()
    rows = conn.execute(
        """
        SELECT lead_id, role, COUNT(*) AS c
        FROM scheduled_callbacks
        WHERE status = 'scheduled'
        GROUP BY lead_id, role
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    removed = 0
    for row in rows:
        lead_id = row["lead_id"]
        role = row["role"]
        dups = conn.execute(
            """
            SELECT id FROM scheduled_callbacks
            WHERE lead_id = ? AND role = ? AND status = 'scheduled'
            ORDER BY scheduled_at ASC
            """,
            (lead_id, role),
        ).fetchall()
        for dup in dups[1:]:
            conn.execute("DELETE FROM scheduled_callbacks WHERE id = ?", (dup["id"],))
            removed += 1
    if removed:
        conn.commit()
    return {"duplicate_callbacks_removed": removed}


async def run_boss_cleanup_sweep(app_root: str | Path | None = None) -> dict[str, Any]:
    """Full hygiene pass — safe during idle or low-call periods."""
    results: dict[str, Any] = {"at": time.time()}
    results["temp"] = cleanup_temp_deploy_artifacts()
    if app_root:
        results["pycache"] = cleanup_pycache_under_app(app_root)
    results["orphaned_dialing"] = await cleanup_orphaned_dialing()
    results["callback_dedup"] = await dedupe_pending_callbacks()
    return results
