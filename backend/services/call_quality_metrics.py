"""Per-call quality metrics collector and storage.

Hooks into live_session.py to capture:
- Response latency (user end → model audio start)
- Greeting-to-first-response delay
- Audio noise floor levels
- Turn-taking quality (barge-ins, silence timeouts)
- Echo / audio issues

Stores in SQLite for the Call Quality Guardian agent to analyze.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from loguru import logger

# ── DB path ───────────────────────────────────────────────────────────────
_QUALITY_DB_DIR = Path(__file__).resolve().parents[2] / "data"
_QUALITY_DB_PATH = _QUALITY_DB_DIR / "call_quality.db"


def _get_conn():
    from core.storage import _get_conn as _main_conn

    conn = _main_conn()
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS call_quality_metrics (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            camp_id       TEXT NOT NULL,
            role          TEXT NOT NULL DEFAULT '',
            phone         TEXT NOT NULL DEFAULT '',
            call_id       TEXT NOT NULL DEFAULT '',
            
            -- Timestamps
            call_connect_at   REAL NOT NULL,
            call_end_at       REAL,
            greeting_finished_at REAL,
            first_model_audio_at REAL,
            nudge_sent_at     REAL,
            
            -- Latency (ms)
            greeting_to_first_response_ms REAL DEFAULT -1,
            user_end_to_model_start_ms    REAL DEFAULT -1,
            avg_response_latency_ms       REAL DEFAULT -1,
            
            -- Audio quality
            avg_noise_floor_rms   REAL DEFAULT -1,
            avg_inbound_rms       REAL DEFAULT -1,
            avg_outbound_rms      REAL DEFAULT -1,
            noise_suppression_active INTEGER DEFAULT 0,
            
            -- Turn-taking
            barge_in_count        INTEGER DEFAULT 0,
            silence_nudge_count   INTEGER DEFAULT 0,
            user_silence_sec      REAL DEFAULT -1,
            total_turns           INTEGER DEFAULT 0,
            
            -- Outcome
            voicemail             INTEGER DEFAULT 0,
            name_confirmed        INTEGER DEFAULT 0,
            pitch_delivered       INTEGER DEFAULT 0,
            call_outcome          TEXT DEFAULT '',
            
            created_at REAL DEFAULT (julianday('now'))
        );
        
        CREATE INDEX IF NOT EXISTS idx_cq_camp ON call_quality_metrics(camp_id);
        CREATE INDEX IF NOT EXISTS idx_cq_role ON call_quality_metrics(role);
        CREATE INDEX IF NOT EXISTS idx_cq_created ON call_quality_metrics(created_at);
    """)


# ── In-memory metrics accumulator ────────────────────────────────────────
# Holds per-call metrics while the call is live. Keyed by camp_id.
_LIVE_METRICS: dict[str, dict[str, Any]] = {}


def init_call_metrics(camp_id: str, role: str = "", phone: str = "") -> None:
    """Create a fresh metrics bucket for a new call."""
    _LIVE_METRICS[camp_id] = {
        "camp_id": camp_id,
        "role": role,
        "phone": phone,
        "call_connect_at": time.perf_counter(),
        "call_end_at": 0.0,
        "greeting_finished_at": 0.0,
        "first_model_audio_at": 0.0,
        "nudge_sent_at": 0.0,
        "greeting_to_first_response_ms": -1.0,
        "user_end_to_model_start_ms": -1.0,
        "avg_response_latency_ms": -1.0,
        "last_user_end_at": 0.0,
        "response_latencies": [],
        "avg_noise_floor_rms": -1.0,
        "noise_floor_samples": [],
        "avg_inbound_rms": -1.0,
        "avg_outbound_rms": -1.0,
        "barge_in_count": 0,
        "silence_nudge_count": 0,
        "user_silence_sec": -1.0,
        "total_turns": 0,
        "voicemail": 0,
        "name_confirmed": 0,
        "pitch_delivered": 0,
        "call_outcome": "",
        "noise_suppression_active": 1,
    }


def record_greeting_finished(camp_id: str) -> None:
    m = _LIVE_METRICS.get(camp_id)
    if m:
        m["greeting_finished_at"] = time.perf_counter()


def record_nudge_sent(camp_id: str) -> None:
    m = _LIVE_METRICS.get(camp_id)
    if m:
        m["nudge_sent_at"] = time.perf_counter()


def record_first_model_audio(camp_id: str) -> None:
    m = _LIVE_METRICS.get(camp_id)
    if m:
        m["first_model_audio_at"] = time.perf_counter()
        # Compute greeting-to-first-response latency
        if m["greeting_finished_at"] > 0 and m["first_model_audio_at"] > 0:
            gap_ms = (m["first_model_audio_at"] - m["greeting_finished_at"]) * 1000.0
            m["greeting_to_first_response_ms"] = round(gap_ms, 1)
            logger.info("📊 Call quality: greeting→response = {:.0f} ms", gap_ms)
        # Also compute nudge-to-response latency
        if m["nudge_sent_at"] > 0:
            nudge_gap = (m["first_model_audio_at"] - m["nudge_sent_at"]) * 1000.0
            logger.info("📊 Call quality: nudge→response = {:.0f} ms", nudge_gap)


def record_user_activity_end(camp_id: str) -> None:
    """Called when activityEnd fires — marks user speech end for latency tracking."""
    m = _LIVE_METRICS.get(camp_id)
    if m:
        m["last_user_end_at"] = time.perf_counter()


def record_response_latency(camp_id: str) -> None:
    """Called when model audio first arrives after an activityEnd."""
    m = _LIVE_METRICS.get(camp_id)
    if not m or m["last_user_end_at"] <= 0:
        return
    latency_ms = (time.perf_counter() - m["last_user_end_at"]) * 1000.0
    m["response_latencies"].append(round(latency_ms, 1))
    m["user_end_to_model_start_ms"] = round(latency_ms, 1)
    if len(m["response_latencies"]) > 0:
        m["avg_response_latency_ms"] = round(
            sum(m["response_latencies"]) / len(m["response_latencies"]), 1
        )


def record_noise_floor(camp_id: str, rms: float) -> None:
    m = _LIVE_METRICS.get(camp_id)
    if m and rms > 0:
        m["noise_floor_samples"].append(rms)


def record_barge_in(camp_id: str) -> None:
    m = _LIVE_METRICS.get(camp_id)
    if m:
        m["barge_in_count"] += 1


def record_silence_nudge(camp_id: str) -> None:
    m = _LIVE_METRICS.get(camp_id)
    if m:
        m["silence_nudge_count"] += 1


def record_turn(camp_id: str) -> None:
    m = _LIVE_METRICS.get(camp_id)
    if m:
        m["total_turns"] += 1


def set_call_outcome(
    camp_id: str,
    *,
    voicemail: bool = False,
    name_confirmed: bool = False,
    pitch_delivered: bool = False,
    outcome: str = "",
) -> None:
    m = _LIVE_METRICS.get(camp_id)
    if m:
        if voicemail:
            m["voicemail"] = 1
        if name_confirmed:
            m["name_confirmed"] = 1
        if pitch_delivered:
            m["pitch_delivered"] = 1
        if outcome:
            m["call_outcome"] = outcome


def finalize_call_metrics(camp_id: str, *, call_id: str = "") -> None:
    """Persist accumulated metrics to DB and clean up from memory."""
    m = _LIVE_METRICS.pop(camp_id, None)
    if not m:
        return
    m["call_end_at"] = time.perf_counter()

    # Compute averages
    if m["noise_floor_samples"]:
        m["avg_noise_floor_rms"] = round(
            sum(m["noise_floor_samples"]) / len(m["noise_floor_samples"]), 2
        )

    # Call duration
    duration = m["call_end_at"] - m["call_connect_at"]
    if m["voicemail"]:
        m["call_outcome"] = m["call_outcome"] or "voicemail"
    elif m["pitch_delivered"]:
        m["call_outcome"] = m["call_outcome"] or "pitch_delivered"
    elif m["name_confirmed"]:
        m["call_outcome"] = m["call_outcome"] or "name_confirmed_only"
    elif duration > 30:
        m["call_outcome"] = m["call_outcome"] or "connected_no_pitch"
    else:
        m["call_outcome"] = m["call_outcome"] or "short_call"

    try:
        conn = _get_conn()
        conn.execute(
            """
            INSERT INTO call_quality_metrics (
                camp_id, role, phone, call_id,
                call_connect_at, call_end_at,
                greeting_finished_at, first_model_audio_at, nudge_sent_at,
                greeting_to_first_response_ms,
                user_end_to_model_start_ms,
                avg_response_latency_ms,
                avg_noise_floor_rms,
                barge_in_count, silence_nudge_count, user_silence_sec,
                total_turns, voicemail, name_confirmed, pitch_delivered,
                call_outcome, noise_suppression_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                m["camp_id"], m["role"], m["phone"], call_id,
                m["call_connect_at"], m["call_end_at"],
                m["greeting_finished_at"], m["first_model_audio_at"], m["nudge_sent_at"],
                m["greeting_to_first_response_ms"],
                m["user_end_to_model_start_ms"],
                m["avg_response_latency_ms"],
                m["avg_noise_floor_rms"],
                m["barge_in_count"], m["silence_nudge_count"], m["user_silence_sec"],
                m["total_turns"], m["voicemail"], m["name_confirmed"], m["pitch_delivered"],
                m["call_outcome"], m["noise_suppression_active"],
            ),
        )
        conn.commit()
        conn.close()

        greeting_lat = m["greeting_to_first_response_ms"]
        avg_lat = m["avg_response_latency_ms"]
        logger.info(
            "📊 Call quality saved: camp={} greet→resp={}ms avg_lat={}ms outcome={} noise_floor={}",
            camp_id[:12],
            greeting_lat if greeting_lat > 0 else -1,
            avg_lat if avg_lat > 0 else -1,
            m["call_outcome"],
            m["avg_noise_floor_rms"] if m["avg_noise_floor_rms"] > 0 else -1,
        )
    except Exception as e:
        logger.warning("📊 Failed to persist quality metrics: {}", e)


# ── Query helpers for the agent ──────────────────────────────────────────

def get_recent_calls(limit: int = 50, *, min_duration_sec: float = 5.0) -> list[dict[str, Any]]:
    """Fetch recent call quality records."""
    try:
        conn = _get_conn()
        rows = conn.execute(
            """
            SELECT * FROM call_quality_metrics
            WHERE (call_end_at - call_connect_at) >= ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (min_duration_sec, limit),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("📊 get_recent_calls failed: {}", e)
        return []


def get_latency_stats(min_samples: int = 5) -> dict[str, Any]:
    """Aggregate latency stats from recent calls."""
    try:
        conn = _get_conn()
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS sample_count,
                ROUND(AVG(greeting_to_first_response_ms)::numeric, 1) AS avg_greeting_lat,
                ROUND(AVG(avg_response_latency_ms)::numeric, 1) AS avg_turn_lat,
                MIN(greeting_to_first_response_ms) AS min_greeting_lat,
                MAX(greeting_to_first_response_ms) AS max_greeting_lat,
                ROUND(AVG(avg_noise_floor_rms)::numeric, 1) AS avg_noise_floor,
                SUM(barge_in_count) AS total_barge_ins,
                SUM(voicemail) AS voicemail_count,
                SUM(name_confirmed) AS name_confirmed_count,
                SUM(pitch_delivered) AS pitch_delivered_count
            FROM call_quality_metrics
            WHERE greeting_to_first_response_ms > 0
        """).fetchone()
        conn.close()
        if row and int(row["sample_count"] or 0) >= min_samples:
            return dict(row)
        return {"sample_count": 0}
    except Exception as e:
        logger.warning("📊 get_latency_stats failed: {}", e)
        return {"sample_count": 0}
