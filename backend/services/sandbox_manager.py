"""Agent Factory Service — SQLite-backed wrapper around core.storage."""

from __future__ import annotations
from typing import Optional
from loguru import logger

from services.rag_processor import extract_text_from_file
from core.storage import (
    _create_agent_sync as db_create_agent,
    _get_agent_sync as db_get_agent,
    _list_agents_sync as db_list_agents,
    _update_agent_sync as db_update_agent,
    _delete_agent_sync as db_delete_agent,
    _add_agent_knowledge_file_sync as db_add_kb,
    _add_agent_lead_sync as db_add_lead,
    _get_agent_leads_sync as db_get_leads
)

def create_agent(name: str, prompt: str, voice: str = "Puck", role: str = "factory") -> dict:
    aid = db_create_agent(name, prompt, voice, role=role)
    return db_get_agent(aid)

def get_agent(agent_id: str) -> Optional[dict]:
    return db_get_agent(agent_id)

def list_agents(role: Optional[str] = None) -> list[dict]:
    return db_list_agents(role=role)

def update_agent(agent_id: str, name: str = None, prompt: str = None, voice: str = None):
    db_update_agent(agent_id, name, prompt, voice)

def delete_agent(agent_id: str) -> bool:
    return db_delete_agent(agent_id)

def add_agent_lead(agent_id: str, lead: dict) -> dict:
    lid = db_add_lead(agent_id, lead)
    return {"lead_id": lid, **lead}

def get_agent_leads(agent_id: str) -> list[dict]:
    return db_get_leads(agent_id)

def associate_file_with_agent(agent_id: str, content: bytes, filename: str) -> Optional[dict]:
    # Process text
    text = extract_text_from_file(content, filename)
    import uuid
    fid = str(uuid.uuid4())
    db_add_kb(agent_id, fid, filename, text)
    return {"file_id": fid, "filename": filename}

def add_agent_knowledge_file(agent_id: str, filename: str, text: str):
    import uuid
    fid = str(uuid.uuid4())
    db_add_kb(agent_id, fid, filename, text)
