"""NDJSON debug logger for agent debug session cbc88a."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

_SESSION_ID = "cbc88a"
_LOG_PATHS = (
    Path(__file__).resolve().parents[1] / "debug-cbc88a.log",  # technopolis/ (VPS: /opt/technopolis/)
    Path(__file__).resolve().parents[2] / "debug-cbc88a.log",  # parent
    Path(__file__).resolve().parents[3] / "debug-cbc88a.log",  # workspace root (local)
)


def agent_debug(
    hypothesis_id: str,
    location: str,
    message: str,
    data: dict[str, Any] | None = None,
    *,
    run_id: str = "pre-fix",
) -> None:
    # #region agent log
    try:
        entry = {
            "sessionId": _SESSION_ID,
            "timestamp": int(time.time() * 1000),
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data or {},
            "runId": run_id,
        }
        line = json.dumps(entry, default=str) + "\n"
        for path in _LOG_PATHS:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(line)
            except Exception:
                pass
    except Exception:
        pass
    # #endregion


def log_path() -> Path:
    return _LOG_PATHS[0]
