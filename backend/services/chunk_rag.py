"""Structured chunk RAG — retrieve factual KB slices before answering."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from loguru import logger

from config import settings

_BACKEND = Path(__file__).resolve().parent.parent
_SECTION_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)


def _chunks_path(role: str) -> Path:
    return _BACKEND / "data" / role / "kb_chunks.json"


def _split_rag_source(text: str) -> list[dict[str, Any]]:
    raw = (text or "").strip()
    if not raw:
        return []
    parts = _SECTION_RE.split(raw)
    if len(parts) < 3:
        return [{"id": "full", "title": "Knowledge Base", "tags": [], "text": raw}]
    chunks: list[dict[str, Any]] = []
    if parts[0].strip():
        chunks.append({"id": "intro", "title": "Introduction", "tags": ["overview"], "text": parts[0].strip()})
    for i in range(1, len(parts), 2):
        title = parts[i].strip()
        body = (parts[i + 1] if i + 1 < len(parts) else "").strip()
        if not body:
            continue
        slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:48] or f"chunk_{len(chunks)}"
        chunks.append({"id": slug, "title": title, "tags": [slug.replace("_", " ")], "text": body})
    return chunks


def _normalize_chunk(chunk: dict[str, Any]) -> dict[str, Any]:
    out = dict(chunk)
    if not str(out.get("text") or "").strip() and out.get("facts"):
        out["text"] = "\n".join(str(f).strip() for f in out.get("facts") or [] if str(f).strip())
    if not out.get("title") and out.get("topic"):
        out["title"] = out.get("topic")
    return out


@lru_cache(maxsize=8)
def load_role_chunks(role: str) -> list[dict[str, Any]]:
    path = _chunks_path(role)
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            raw: list[dict[str, Any]] = []
            if isinstance(data, list) and data:
                raw = [c for c in data if isinstance(c, dict)]
            elif isinstance(data, dict) and isinstance(data.get("chunks"), list):
                raw = [c for c in data["chunks"] if isinstance(c, dict)]
            if raw:
                return [_normalize_chunk(c) for c in raw if _normalize_chunk(c).get("text")]
        except Exception as exc:
            logger.warning("Failed to load {}: {}", path, exc)
    from prompts.role_prompts import get_role_rag_source_text

    return _split_rag_source(get_role_rag_source_text(role))


def rebuild_role_kb_chunks(role: str) -> int:
    """Regenerate data/{role}/kb_chunks.json from the live rag_source.txt and
    drop the cached chunks so the next call uses the freshly saved KB."""
    from prompts.role_prompts import get_role_rag_source_text
    from scripts.build_kb_chunks import build_chunks_from_rag

    # Always drop the in-memory chunk cache first — even when the KB is
    # cleared, the previous chunk list must never leak into the next call.
    load_role_chunks.cache_clear()

    text = get_role_rag_source_text(role)
    out = _chunks_path(role)
    if not text:
        # Operator cleared the KB from the dashboard: remove the stale chunk
        # file so loaders fall back to the (now empty) rag_source.txt.
        try:
            if out.is_file():
                out.unlink()
        except Exception as exc:
            logger.warning("Failed to remove stale kb_chunks for role={}: {}", role, exc)
        return 0
    chunks = build_chunks_from_rag(text)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"role": role, "version": 1, "chunks": chunks}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return len(chunks)


def overview_chunk(role: str) -> str:
    chunks = load_role_chunks(role)
    prefer = ("overview", "intro", "project_overview", "configurations_pricing", "configurations")
    for pid in prefer:
        for c in chunks:
            if c.get("id") == pid:
                return str(c.get("text") or "").strip()[:600]
    if chunks:
        return str(chunks[0].get("text") or "").strip()[:600]
    return ""


def _score_chunk(query: str, chunk: dict[str, Any]) -> int:
    q = (query or "").lower()
    if len(q) < 2:
        return 0
    title = str(chunk.get("title") or chunk.get("topic") or "").lower()
    tags = " ".join(str(t) for t in (chunk.get("tags") or [])).lower()
    body = str(chunk.get("text") or "").lower()
    if not body and chunk.get("facts"):
        body = " ".join(str(f) for f in chunk.get("facts") or []).lower()
    score = 0
    for tok in re.findall(r"[a-z0-9]{3,}", q):
        if tok in title:
            score += 4
        if tok in tags:
            score += 3
        if tok in body:
            score += 1
    for kw, boost in (
        ("price", 6), ("pricing", 6), ("crore", 5), ("bhk", 5), ("cost", 4),
        ("location", 5), ("amenit", 4), ("phase", 4), ("possession", 4),
        ("visit", 3), ("whatsapp", 2), ("brochure", 2),
    ):
        if kw in q and kw in body:
            score += boost
    try:
        from rapidfuzz import fuzz

        fuzzy_ratio = fuzz.token_set_ratio(q, title)
        if fuzzy_ratio >= 80:
            score += 5
        if fuzzy_ratio >= 60:
            score += 2
        tag_ratio = fuzz.token_set_ratio(q, tags)
        if tag_ratio >= 80:
            score += 3
        body_ratio = fuzz.partial_ratio(q, body[:1500])
        if body_ratio >= 85:
            score += 3
        elif body_ratio >= 65:
            score += 1
        # Telugu/Tenglish tokens are often romanized differently — boost on
        # partial overlap so natural-language phrasing still finds the chunk.
        if any(len(t) >= 4 and fuzz.partial_ratio(t, body) >= 70 for t in re.findall(r"[a-z0-9]{4,}", q)):
            score += 2
    except Exception:
        pass
    return score


def retrieve_chunks(
    query: str,
    role: str,
    *,
    top_k: int | None = None,
    max_chars: int | None = None,
) -> list[dict[str, str]]:
    k = top_k if top_k is not None else int(getattr(settings, "rag_chunk_top_k", 3) or 3)
    cap = max_chars if max_chars is not None else int(getattr(settings, "rag_chunk_max_chars", 1200) or 1200)
    chunks = load_role_chunks(role)
    if not chunks:
        return []
    ranked = sorted(chunks, key=lambda c: _score_chunk(query, c), reverse=True)
    out: list[dict[str, str]] = []
    used = 0
    for c in ranked:
        if _score_chunk(query, c) <= 0 and out:
            break
        txt = str(c.get("text") or "").strip()
        if not txt and c.get("facts"):
            txt = "\n".join(f"- {f}" for f in c.get("facts") if str(f).strip())
        if not txt:
            continue
        remain = cap - used
        if remain <= 0:
            break
        if len(txt) > remain:
            txt = txt[: max(0, remain - 1)].rstrip() + "…"
        out.append({"id": str(c.get("id") or ""), "title": str(c.get("title") or c.get("topic") or ""), "text": txt})
        used += len(txt)
        if len(out) >= k:
            break
    if not out and chunks:
        ov = overview_chunk(role)
        if ov:
            out.append({"id": "overview", "title": "Overview", "text": ov[: min(cap, 800)]})
    return out


def format_chunk_context(chunks: list[dict[str, str]], *, header: str = "[SYSTEM RAG CONTEXT]") -> str:
    if not chunks:
        return ""
    lines = [header, "Use ONLY these facts for pricing, configs, location, amenities, and timelines."]
    for i, c in enumerate(chunks, 1):
        title = c.get("title") or c.get("id") or f"chunk_{i}"
        lines.append(f"\n--- [{i}] {title} ---\n{c.get('text', '').strip()}")
    return "\n".join(lines).strip()


def rag_mode() -> str:
    return (getattr(settings, "rag_mode", None) or "chunk").strip().lower()


def is_chunk_rag() -> bool:
    return rag_mode() in ("chunk", "chunks", "structured")


def looks_factual_question(text: str) -> bool:
    q = (text or "").lower()
    if len(q.strip()) < 4:
        return False
    if "?" in q:
        return True
    return bool(
        re.search(
            r"\b(price|pricing|cost|crore|lakh|bhk|location|where|amenit|phase|"
            r"possession|timeline|payment|loan|rera|visit|brochure|config|sq\.?\s*ft|"
            r"backyard|clubhouse|pool|parking)\b",
            q,
        )
    )


def full_chunk_block(role: str, *, max_chars: int = 30000) -> str:
    """Embed the ENTIRE role KB (raw rag_source.txt, up to ``max_chars``) at connect.

    The dashboard RAG is the single source of truth — whatever the user pastes
    becomes the full knowledge block verbatim (no per-chunk truncation), so the
    model can answer ANY question about the project with zero per-turn retrieval.
    Falls back to the chunk json only if no rag_source.txt exists.
    """
    from prompts.role_prompts import get_role_rag_source_text

    raw = get_role_rag_source_text(role).strip()
    if not raw:
        chunks = load_role_chunks(role)
        if not chunks:
            return ""
        out: list[dict[str, str]] = []
        used = 0
        for c in chunks:
            txt = str(c.get("text") or "").strip()
            if not txt and c.get("facts"):
                txt = "\n".join(f"- {f}" for f in c.get("facts") if str(f).strip())
            if not txt:
                continue
            remain = max_chars - used
            if remain <= 0:
                break
            if len(txt) > remain:
                txt = txt[: max(0, remain - 1)].rstrip() + "…"
            out.append(
                {
                    "id": str(c.get("id") or ""),
                    "title": str(c.get("title") or c.get("topic") or ""),
                    "text": txt,
                }
            )
            used += len(txt)
        return format_chunk_context(out, header="[KNOWLEDGE BASE — AUTHORITATIVE FACTS AT CONNECT]")

    if len(raw) > max_chars:
        raw = raw[: max(0, max_chars - 1)].rstrip() + "\n…(KB continues)"
    return (
        "[KNOWLEDGE BASE — AUTHORITATIVE FACTS AT CONNECT]\n"
        "Use ONLY these facts for pricing, configs, location, amenities, and timelines. "
        "Never invent numbers, dates, or claims not present here.\n"
        "\n"
        + raw
    )


def connect_digest_for_role(role: str, *, max_chars: int = 30000) -> str:
    """Compact full-KB block for the system prompt at connect.

    Embeds the complete sales_1 chunk set so the model has every fact on
    every turn — no per-question retrieval needed (zero hot-path latency).
    """
    return full_chunk_block(role, max_chars=max_chars)

