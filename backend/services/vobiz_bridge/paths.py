"""Repo paths for the bridge package (``backend/`` root)."""

from __future__ import annotations

from pathlib import Path


def backend_dir() -> Path:
    """``backend/`` directory (parent of ``services/vobiz_bridge``)."""
    return Path(__file__).resolve().parents[2]
