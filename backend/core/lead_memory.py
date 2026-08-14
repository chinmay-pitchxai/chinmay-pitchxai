"""Read back a lead's persistent memory for injection into a live conversation.

This is the read-side complement to ``orchestration_service.update_memory``
(write side). The write side stores facts + summary in the ``lead_memory``
table. Every outbound call should read this compact summary so the AI can
behave as if it remembers the lead instead of starting fresh.

Nothing here changes the write path, the persona, the opening line, or the
dialogue rules — it only produces a short read-only context string that callers
(freeze-safe) append as reference facts.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any


def get_memory(conn: sqlite3.Connection, lead_id: int) -> dict[str, Any]:
    """Return the stored memory for a lead, or an empty dict if none."""
    try:
        row = conn.execute(
            "SELECT facts_json, summary, last_interaction_at, version "
            "FROM lead_memory WHERE lead_id=?",
            (lead_id,),
        ).fetchone()
    except sqlite3.Error:
        return {}
    if not row:
        return {}
    # Work with both sqlite3.Row (name + position) and the Postgres shim's
    # DictRow (name + position). Positional fallback must map to the SELECT
    # order above — summary is index 1, version is index 3.
    def _cell(name: str, index: int):
        try:
            return row[name]
        except (KeyError, IndexError, TypeError):
            return row[index]

    facts = _cell("facts_json", 0)
    summary = _cell("summary", 1)
    version = _cell("version", 3)
    try:
        facts = json.loads(facts or "{}")
    except (ValueError, TypeError):
        facts = {}
    if not isinstance(facts, dict):
        facts = {}

    return {
        "facts": facts,
        "summary": (summary or "").strip(),
        "version": int(version or 0),
    }


def memory_context(conn: sqlite3.Connection, lead_id: int, max_chars: int = 1400) -> str:
    """Build a compact, read-only context block for the conversation prompt.

    Empty results return "" so callers can skip appending anything.
    """
    mem = get_memory(conn, lead_id)
    if not mem and not mem.get("facts") and not mem.get("summary"):
        return ""

    lines: list[str] = []
    if mem.get("summary"):
        lines.append(mem["summary"][:max_chars])

    facts = mem.get("facts") or {}
    # Surface the most decision-relevant fields first, trimmed per value.
    priority_keys = [
        "budget", "preferred_area", "location", "project", "configuration",
        "unit", "bhk", "family", "occupation", "purpose", "objections",
        "questions", "decision_maker", "timeline", "loan_need", "competitor",
        "callback_reason", "follow_up_reason", "interest", "price",
    ]
    ordered: list[str] = []
    for key in priority_keys:
        if key in facts and facts[key] not in (None, "", [], {}):
            ordered.append(f"{key}: {facts[key]}")
    for key, val in facts.items():
        if key in priority_keys or val in (None, "", [], {}):
            continue
        ordered.append(f"{key}: {val}")

    # Fit the fact list within the character budget (summary already took some).
    budget = max_chars - len(" ".join(lines))
    if budget > 40:
        for line in ordered:
            if budget <= 0:
                break
            take = line[:600] + ("..." if len(line) > 600 else "")
            lines.append(take)
            budget -= len(take)

    if not lines:
        return ""
    body = "\n".join(lines)
    return (
        "\n\n[REMEMBERED FROM YOUR PREVIOUS CONVERSATIONS WITH THIS PERSON]\n"
        "Use these facts to keep continuity — do not repeat questions the person "
        "already answered, and reference prior topics naturally when relevant. "
        "Do not invent facts that are not listed here; the list is what you actually know."
        "\n" + body
    )
