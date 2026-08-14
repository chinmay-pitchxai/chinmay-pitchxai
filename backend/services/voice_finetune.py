"""Persistent voice dev-mode fine-tuning — additive rules for future calls.

After a dev-mode call ends, instructions from ``dev_mode_instructions.jsonl`` are
merged into ``backend/data/voice_finetune.md``. Every new call loads that overlay
into the system prompt (no redeploy / restart required for prompt rules).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from services.vobiz_bridge.paths import backend_dir

FINETUNE_FILENAME = "voice_finetune.md"
INSTRUCTIONS_FILENAME = "dev_mode_instructions.jsonl"
STATE_FILENAME = "voice_finetune_state.json"


def _data_dir() -> Path:
    d = backend_dir() / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def finetune_path() -> Path:
    return _data_dir() / FINETUNE_FILENAME


def _instructions_path() -> Path:
    return _data_dir() / INSTRUCTIONS_FILENAME


def _state_path() -> Path:
    return _data_dir() / STATE_FILENAME


def _dev_mode_codeword() -> str:
    return (os.getenv("DEV_MODE_CODEWORD") or "panther chinmay").strip().lower()


def _normalize_rule(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _entry_key(entry: dict[str, Any]) -> str:
    return "|".join(
        [
            str(entry.get("call_id") or ""),
            str(entry.get("timestamp") or ""),
            _normalize_rule(str(entry.get("instruction") or ""))[:120],
        ]
    )


def _load_state() -> dict[str, Any]:
    path = _state_path()
    if not path.is_file():
        return {"applied_entry_keys": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"applied_entry_keys": []}
        keys = data.get("applied_entry_keys")
        if not isinstance(keys, list):
            keys = []
        return {"applied_entry_keys": keys}
    except Exception as exc:
        logger.warning("voice_finetune state load failed: {}", exc)
        return {"applied_entry_keys": []}


def _save_state(state: dict[str, Any]) -> None:
    _state_path().write_text(json.dumps(state, indent=2), encoding="utf-8")


def _load_existing_rules() -> set[str]:
    path = finetune_path()
    if not path.is_file():
        return set()
    rules: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        # Strip leading "- [date] " prefix for dedupe
        body = re.sub(r"^-\s*\[[^\]]+\]\s*", "", line).strip()
        body = re.sub(r"\s*\(call [^)]+\)\s*$", "", body).strip()
        norm = _normalize_rule(body)
        if norm:
            rules.add(norm)
    return rules


def _should_skip_instruction(text: str) -> bool:
    t = (text or "").strip()
    if len(t) < 10:
        return True
    low = t.lower()
    codeword = _dev_mode_codeword()
    if low == codeword or low.replace("-", " ") == codeword:
        return True
    # Codeword-only utterances (STT variants)
    if "panther" in low and "chinmay" in low and len(low.split()) <= 6:
        if not any(w in low for w in ("don't", "stop", "fix", "change", "slow", "pitch", "whatsapp", "visit", "robot")):
            return True
    noise = (
        "developer mode",
        "enter developer",
        "panther code",
        "dev mode",
        "apply changes",
    )
    if any(n in low for n in noise) and len(low) < 40:
        return True
    return False


def load_voice_finetune_overlay() -> str:
    """Return system-prompt block from persistent fine-tune file (empty if none)."""
    path = finetune_path()
    if not path.is_file():
        return ""
    body = path.read_text(encoding="utf-8").strip()
    if not body:
        return ""
    return (
        "\n\n[VOICE DEVELOPER FINE-TUNE — PERSISTENT RULES]\n"
        "These rules were set by the authorized developer on a prior dev-mode call. "
        "Follow them on every call; they fine-tune (not replace) the sales flow below.\n\n"
        f"{body}\n"
    )


def _read_instructions_for_call(call_id: str) -> list[dict[str, Any]]:
    path = _instructions_path()
    if not path.is_file() or not call_id:
        return []
    out: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            if str(entry.get("call_id") or "") != call_id:
                continue
            out.append(entry)
    except Exception as exc:
        logger.warning("voice_finetune read instructions failed: {}", exc)
    return out


def apply_dev_finetune_for_call(call_id: str) -> dict[str, int]:
    """Merge unapplied dev-mode instructions for *call_id* into voice_finetune.md."""
    if not call_id:
        return {"applied": 0, "skipped": 0}

    state = _load_state()
    applied_keys: set[str] = set(state.get("applied_entry_keys") or [])
    existing_rules = _load_existing_rules()
    entries = _read_instructions_for_call(call_id)

    applied = 0
    skipped = 0
    new_lines: list[str] = []

    for entry in entries:
        key = _entry_key(entry)
        if key in applied_keys:
            skipped += 1
            continue
        instruction = str(entry.get("instruction") or "").strip()
        if _should_skip_instruction(instruction):
            applied_keys.add(key)
            skipped += 1
            continue
        norm = _normalize_rule(instruction)
        if norm in existing_rules:
            applied_keys.add(key)
            skipped += 1
            continue

        ts = str(entry.get("timestamp") or "")
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            date_label = dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            date_label = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        new_lines.append(f"- [{date_label}] {instruction} (call {call_id})")
        existing_rules.add(norm)
        applied_keys.add(key)
        applied += 1

    if new_lines:
        path = finetune_path()
        if path.is_file() and path.read_text(encoding="utf-8").strip():
            prefix = path.read_text(encoding="utf-8").rstrip() + "\n"
        else:
            prefix = (
                "# Voice developer fine-tune rules\n"
                "# Set via phone dev mode (panther chinmay). Loaded on every call.\n\n"
            )
        path.write_text(prefix + "\n".join(new_lines) + "\n", encoding="utf-8")
        logger.info(
            "Voice fine-tune: applied {} rule(s) from call {} → {}",
            applied,
            call_id,
            path,
        )

    state["applied_entry_keys"] = sorted(applied_keys)[-5000:]
    _save_state(state)
    return {"applied": applied, "skipped": skipped}


async def apply_dev_finetune_for_call_async(call_id: str) -> dict[str, int]:
    return await asyncio.to_thread(apply_dev_finetune_for_call, call_id)
