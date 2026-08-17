"""Validated production catalog for the four-sandbox calling pipeline."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any


CATALOG_PATH = Path(__file__).resolve().parent.parent / "sandbox" / "agents.json"
EXPECTED_SANDBOXES = (1, 2, 3, 4)
EXPECTED_LINES = tuple(f"P{i}" for i in range(1, 10))


class SandboxCatalogError(RuntimeError):
    """Raised when the deploy-time sandbox contract is unsafe or incomplete."""


@lru_cache(maxsize=1)
def load_sandbox_catalog() -> tuple[dict[str, Any], ...]:
    try:
        raw = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SandboxCatalogError(f"Cannot load sandbox catalog {CATALOG_PATH}: {exc}") from exc
    if not isinstance(raw, list):
        raise SandboxCatalogError("sandbox/agents.json must contain a JSON array")

    required = {"id", "sandbox", "name", "role", "purpose", "phone_lines", "job_types", "prompt"}
    by_number: dict[int, dict[str, Any]] = {}
    job_owner: dict[str, int] = {}
    line_owner: dict[str, int] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise SandboxCatalogError("Every sandbox catalog item must be an object")
        missing = sorted(required - set(item))
        if missing:
            raise SandboxCatalogError(f"Sandbox entry is missing: {', '.join(missing)}")
        number = int(item["sandbox"])
        if number in by_number:
            raise SandboxCatalogError(f"Duplicate sandbox {number}")
        by_number[number] = item
        if not str(item["prompt"]).strip():
            raise SandboxCatalogError(f"Sandbox {number} has an empty prompt")
        for line in item["phone_lines"]:
            if line not in EXPECTED_LINES:
                raise SandboxCatalogError(f"Sandbox {number} uses unknown line {line}")
            if line in line_owner:
                raise SandboxCatalogError(f"Line {line} belongs to two sandboxes")
            line_owner[line] = number
        for job_type in item["job_types"]:
            if job_type in job_owner:
                raise SandboxCatalogError(f"Job type {job_type} belongs to two sandboxes")
            job_owner[job_type] = number

    if tuple(sorted(by_number)) != EXPECTED_SANDBOXES:
        raise SandboxCatalogError("Exactly Sandboxes 1, 2, 3 and 4 are required")
    if tuple(sorted(line_owner, key=lambda value: int(value[1:]))) != EXPECTED_LINES:
        missing = sorted(set(EXPECTED_LINES) - set(line_owner))
        raise SandboxCatalogError(f"Every line P1-P9 must have one owner; missing={missing}")
    from core.workflow_models import JobType

    expected_jobs = {job_type.value for job_type in JobType}
    configured_jobs = set(job_owner)
    if configured_jobs != expected_jobs:
        missing = sorted(expected_jobs - configured_jobs)
        unknown = sorted(configured_jobs - expected_jobs)
        raise SandboxCatalogError(
            f"Sandbox job ownership mismatch; missing={missing}, unknown={unknown}"
        )
    return tuple(by_number[number] for number in EXPECTED_SANDBOXES)


def sandbox_for_job(job_type: str) -> dict[str, Any]:
    value = str(job_type or "").strip()
    for sandbox in load_sandbox_catalog():
        if value in sandbox["job_types"]:
            return sandbox
    raise SandboxCatalogError(f"No sandbox owns job type {value!r}")


def prompt_overlay_for_job(job_type: str) -> str:
    return str(sandbox_for_job(job_type)["prompt"]).strip()


def public_sandbox_catalog() -> list[dict[str, Any]]:
    """Return a detached JSON-safe catalog for APIs and dashboards."""
    return json.loads(json.dumps(load_sandbox_catalog()))


def sync_catalog_agents(conn=None) -> int:
    """Upsert the four managed sandbox agents into the application database.

    The Sandbox/Agent Factory UI reads the ``agents`` table, not the JSON file.
    Deterministic catalog IDs make this safe and idempotent on every startup;
    custom user-created agents are never modified.
    """
    if conn is None:
        from core.storage import _get_conn

        conn = _get_conn()
    synced = 0
    for agent in load_sandbox_catalog():
        conn.execute(
            """INSERT INTO agents(id,role,name,prompt,voice)
            VALUES(?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
              role=excluded.role,
              name=excluded.name,
              prompt=excluded.prompt,
              voice=excluded.voice,
              updated_at=datetime('now')""",
            (
                str(agent["id"]),
                str(agent.get("role") or "sales_1"),
                str(agent["name"]),
                str(agent["prompt"]),
                str(agent.get("voice") or "Puck"),
            ),
        )
        synced += 1
    conn.commit()
    return synced
