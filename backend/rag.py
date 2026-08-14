"""Simple RAG store backed by the primary PostgreSQL database.

Keyword retrieval is done with case-insensitive substring matching (no FTS5),
which is more than sufficient for the small project knowledge base and keeps
the stack on a single PostgreSQL database.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable


_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-]{1,}")


def _fts_terms(text: str, max_terms: int = 24) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for tok in _TOKEN_RE.findall(text or ""):
        t = tok.lower()
        if t in seen:
            continue
        seen.add(t)
        terms.append(t)
        if len(terms) >= max_terms:
            break
    return terms


def _chunk_text(text: str, chunk_chars: int = 900, overlap_chars: int = 180) -> list[str]:
    raw = (text or "").strip()
    if not raw:
        return []
    if len(raw) <= chunk_chars:
        return [raw]

    chunks: list[str] = []
    start = 0
    n = len(raw)
    while start < n:
        end = min(start + chunk_chars, n)
        part = raw[start:end].strip()
        if part:
            chunks.append(part)
        if end >= n:
            break
        start = max(start + 1, end - overlap_chars)
    return chunks


def _conn():
    from core.storage import _get_conn

    return _get_conn()


class RagStore:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        self._init_db()

    def _init_db(self) -> None:
        conn = _conn()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chunks (
                id BIGSERIAL PRIMARY KEY,
                source TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                text TEXT NOT NULL
            )
            """
        )
        conn.commit()

    def clear(self) -> None:
        conn = _conn()
        conn.execute("DELETE FROM chunks")
        conn.commit()

    def add_document(self, source: str, text: str, *, chunk_chars: int = 900) -> int:
        chunks = _chunk_text(text, chunk_chars=chunk_chars)
        if not chunks:
            return 0
        conn = _conn()
        for i, c in enumerate(chunks):
            conn.execute(
                "INSERT INTO chunks(source, chunk_index, text) VALUES (?, ?, ?)",
                (source, i, c),
            )
        conn.commit()
        return len(chunks)

    def build_from_files(self, files: Iterable[Path], *, chunk_chars: int = 900) -> int:
        total = 0
        self.clear()
        for path in files:
            try:
                txt = path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            src = str(path)
            total += self.add_document(src, txt, chunk_chars=chunk_chars)
        return total

    def query(self, text: str, *, top_k: int = 4, max_chars: int = 2200) -> list[dict[str, str]]:
        terms = _fts_terms(text)
        if not terms:
            return []
        conn = _conn()
        rows = conn.execute("SELECT source, text FROM chunks").fetchall()

        # Rank by number of matched terms (case-insensitive substring match).
        scored: list[tuple[int, str, str]] = []
        for r in rows:
            txt = str(r["text"] or "")
            if not txt.strip():
                continue
            low = txt.lower()
            score = sum(1 for t in terms if t in low)
            if score > 0:
                scored.append((score, str(r["source"] or ""), txt))
        scored.sort(key=lambda x: -x[0])

        out: list[dict[str, str]] = []
        used = 0
        for _score, source, txt in scored[:top_k]:
            snippet = txt.strip()
            if not snippet:
                continue
            remain = max_chars - used
            if remain <= 0:
                break
            if len(snippet) > remain:
                snippet = snippet[: max(0, remain - 1)].rstrip() + "…"
            out.append({"source": source, "text": snippet})
            used += len(snippet)
        return out


def format_references(items: list[dict[str, str]]) -> str:
    if not items:
        return ""
    lines: list[str] = []
    for i, item in enumerate(items, start=1):
        src = Path(item.get("source", "")).name or item.get("source", "unknown")
        txt = item.get("text", "").strip()
        if not txt:
            continue
        lines.append(f"[{i}] source={src}\n{txt}")
    return "\n\n".join(lines).strip()
