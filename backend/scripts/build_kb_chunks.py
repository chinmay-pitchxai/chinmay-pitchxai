#!/usr/bin/env python3
"""Build data/{role}/kb_chunks.json from rag_source.txt (# section headers)."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from prompts.role_prompts import get_role_rag_source_text  # noqa: E402

_SECTION_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
_BULLET_RE = re.compile(r"^[\-\*]\s+(.+)$", re.MULTILINE)


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:56] or "chunk"


def _facts_from_body(title: str, body: str) -> list[str]:
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    facts: list[str] = []
    for ln in lines:
        m = _BULLET_RE.match(ln)
        if m:
            facts.append(m.group(1).strip())
        elif ":" in ln and not ln.startswith("#"):
            facts.append(ln)
        elif "**" in ln or len(ln) > 20:
            facts.append(ln)
    if not facts and body.strip():
        facts = [body.strip()[:500]]
    if title and facts:
        facts[0] = f"{title}: {facts[0]}" if not facts[0].lower().startswith(title.lower()[:8]) else facts[0]
    return facts[:12]


def _question_patterns(title: str, tags: list[str]) -> list[str]:
    t = title.lower()
    pats = [t]
    if "pricing" in t or "config" in t:
        pats.extend(["price", "pricing", "cost", "crore", "bhk", "how much"])
    if "location" in t:
        pats.extend(["where", "location", "address", "connectivity"])
    if "phase" in t:
        pats.extend(["phase 3", "townhouse", "duplex", "2.5 crore"])
    if "amenit" in t:
        pats.extend(["amenities", "clubhouse", "pool", "gym"])
    if "objection" in t:
        pats.extend(["too expensive", "too far", "not interested"])
    pats.extend(tags)
    return list(dict.fromkeys(pats))[:10]


def build_chunks_from_rag(text: str) -> list[dict]:
    raw = (text or "").strip()
    if not raw:
        return []
    parts = _SECTION_RE.split(raw)
    chunks: list[dict] = []
    if parts[0].strip():
        intro = parts[0].strip()
        derived = [
            w for w in re.findall(r"[a-z0-9]{4,}", intro.lower())
            if w not in ("project", "overview")
        ][:4]
        chunks.append(
            {
                "id": "overview",
                "topic": "Project Overview",
                "tags": list(dict.fromkeys(["overview"] + derived)),
                "facts": _facts_from_body("Overview", intro),
                "question_patterns": [
                    "tell me about the project",
                    "tell me about yourself",
                    "project details",
                    "what do you sell",
                    "what is this about",
                    "give me details",
                ],
            }
        )
    for i in range(1, len(parts), 2):
        title = parts[i].strip()
        body = (parts[i + 1] if i + 1 < len(parts) else "").strip()
        if not body:
            continue
        slug = _slug(title)
        tags = [w for w in re.findall(r"[a-z0-9]{4,}", title.lower())][:6]
        chunks.append(
            {
                "id": slug,
                "topic": title,
                "tags": tags,
                "facts": _facts_from_body(title, body),
                "question_patterns": _question_patterns(title, tags),
            }
        )
    return chunks


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--roles", default="sales_1")
    args = parser.parse_args()
    for role in [r.strip() for r in args.roles.split(",") if r.strip()]:
        text = get_role_rag_source_text(role)
        if not text:
            print(f"skip {role}: no rag_source.txt")
            continue
        chunks = build_chunks_from_rag(text)
        out = ROOT / "data" / role / "kb_chunks.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {"role": role, "version": 1, "chunks": chunks}
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"{role}: wrote {len(chunks)} chunks -> {out}")


if __name__ == "__main__":
    main()
