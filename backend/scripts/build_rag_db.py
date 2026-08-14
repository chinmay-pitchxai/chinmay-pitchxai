#!/usr/bin/env python3
"""Build local RAG SQLite DB from project knowledge/docs files.

Run on pod:
  cd /workspace/technopolis-agent
  python scripts/build_rag_db.py
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rag import RagStore  # noqa: E402


DEFAULT_SOURCE_DIRS = [
    ROOT / "prompts",
    ROOT / "data" / "sales_1",
]
DEFAULT_FILE_GLOBS = ("*.md", "*.txt", "*.rst", "*.html")


def gather_files(source_dirs: list[Path], globs: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    seen: set[Path] = set()
    for base in source_dirs:
        if not base.exists():
            continue
        for g in globs:
            for p in base.rglob(g):
                if p.is_file() and p not in seen:
                    seen.add(p)
                    files.append(p)
    return files


def _index_kb_chunks(store: RagStore, role_dir: Path) -> int:
    """Index structured kb_chunks.json (facts per topic) into FTS."""
    path = role_dir / "kb_chunks.json"
    if not path.is_file():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    chunks = data.get("chunks") if isinstance(data, dict) else data
    if not isinstance(chunks, list):
        return 0
    total = 0
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        cid = str(chunk.get("id") or "chunk")
        topic = str(chunk.get("topic") or cid)
        facts = chunk.get("facts") or []
        body = "\n".join(str(f) for f in facts if str(f).strip())
        if not body.strip():
            body = str(chunk.get("text") or "")
        if not body.strip():
            continue
        tags = " ".join(str(t) for t in (chunk.get("tags") or []))
        text = f"{topic}\n{tags}\n{body}".strip()
        total += store.add_document(f"{path.name}#{cid}", text, chunk_chars=max(400, len(text) + 1))
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description="Build local RAG DB")
    parser.add_argument("--db", default=str(ROOT / "data" / "rag.db"))
    parser.add_argument("--source-dir", action="append", default=[])
    parser.add_argument("--chunk-chars", type=int, default=900)
    args = parser.parse_args()

    src_dirs = [Path(p).resolve() for p in args.source_dir] if args.source_dir else DEFAULT_SOURCE_DIRS
    store = RagStore(args.db)
    store.clear()
    chunk_total = 0
    for role_dir in (ROOT / "data" / "sales_1",):
        chunk_total += _index_kb_chunks(store, role_dir)
    files = [f for f in gather_files(src_dirs, DEFAULT_FILE_GLOBS) if f.name != "kb_chunks.json"]
    file_total = 0
    for path in files:
        try:
            txt = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        file_total += store.add_document(str(path), txt, chunk_chars=max(400, int(args.chunk_chars)))
    total = chunk_total + file_total
    if total <= 0:
        raise SystemExit("No kb_chunks.json or source files found to index.")

    print(f"RAG DB: {args.db}")
    print(f"Indexed structured chunks: {chunk_total}")
    print(f"Indexed file chunks: {file_total} ({len(files)} files)")
    print(f"Indexed chunks total: {total}")


if __name__ == "__main__":
    main()
