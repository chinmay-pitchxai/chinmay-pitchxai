"""Vobiz Application API — create/attach applications for incoming call routing."""

from __future__ import annotations

from typing import Any, Optional

import httpx
from loguru import logger

_VOBIZ_API_BASE = "https://api.vobiz.ai/api/v1"


def _headers(auth_id: str, auth_token: str) -> dict[str, str]:
    return {
        "X-Auth-ID": auth_id,
        "X-Auth-Token": auth_token,
        "Content-Type": "application/json",
    }


async def create_application(
    auth_id: str,
    auth_token: str,
    *,
    friendly_name: str,
    voice_url: str,
    voice_method: str = "POST",
    hangup_url: str = "",
    hangup_method: str = "POST",
) -> dict[str, Any]:
    """Create a Vobiz Application for incoming call routing.

    Returns the application object (including ``app_id``).
    """
    url = f"{_VOBIZ_API_BASE}/Account/{auth_id}/Application/"
    body: dict[str, Any] = {
        "app_name": friendly_name,
        "answer_url": voice_url,
        "answer_method": voice_method,
    }
    if hangup_url:
        body["hangup_url"] = hangup_url
        body["hangup_method"] = hangup_method

    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(url, json=body, headers=_headers(auth_id, auth_token))
        data: dict[str, Any] = r.json()
        logger.info("Vobiz create_application {} -> HTTP {} body={} resp={}", friendly_name, r.status_code, body, data)
        if r.status_code >= 400:
            raise RuntimeError(f"Vobiz create_application failed: {data}")
        return data


async def list_applications(auth_id: str, auth_token: str) -> list[dict[str, Any]]:
    """List all Vobiz Applications for this account."""
    url = f"{_VOBIZ_API_BASE}/Account/{auth_id}/Application/"
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(url, headers=_headers(auth_id, auth_token))
        data: dict[str, Any] = r.json()
        if r.status_code >= 400:
            raise RuntimeError(f"Vobiz list_applications failed: {data}")
        return data.get("objects") or []


async def get_application(auth_id: str, auth_token: str, app_id: str) -> dict[str, Any]:
    """Retrieve a single Vobiz Application by ID."""
    url = f"{_VOBIZ_API_BASE}/Account/{auth_id}/Application/{app_id}/"
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(url, headers=_headers(auth_id, auth_token))
        data: dict[str, Any] = r.json()
        if r.status_code >= 400:
            raise RuntimeError(f"Vobiz get_application failed: {data}")
        return data


async def update_application(
    auth_id: str,
    auth_token: str,
    app_id: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Update an existing Vobiz Application."""
    url = f"{_VOBIZ_API_BASE}/Account/{auth_id}/Application/{app_id}/"
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(url, json=kwargs, headers=_headers(auth_id, auth_token))
        data: dict[str, Any] = r.json()
        logger.info("Vobiz update_application {} -> HTTP {} {}", app_id, r.status_code, data)
        if r.status_code >= 400:
            raise RuntimeError(f"Vobiz update_application failed: {data}")
        return data


async def delete_application(auth_id: str, auth_token: str, app_id: str) -> dict[str, Any]:
    """Delete a Vobiz Application."""
    url = f"{_VOBIZ_API_BASE}/Account/{auth_id}/Application/{app_id}/"
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.delete(url, headers=_headers(auth_id, auth_token))
        data: dict[str, Any] = r.json()
        logger.info("Vobiz delete_application {} -> HTTP {} {}", app_id, r.status_code, data)
        if r.status_code >= 400:
            raise RuntimeError(f"Vobiz delete_application failed: {data}")
        return data


async def attach_number_to_application(
    auth_id: str,
    auth_token: str,
    app_id: str,
    phone_number: str,
) -> dict[str, Any]:
    """Attach a phone number (DID) to a Vobiz Application for incoming call routing.

    Endpoint: POST /Account/{auth_id}/numbers/{url_encoded_number}/application
    Body: {"application_id": app_id}

    Returns an empty dict on success (HTTP 204 No Content).
    """
    from urllib.parse import quote

    encoded_number = quote(phone_number, safe="")
    url = f"{_VOBIZ_API_BASE}/Account/{auth_id}/numbers/{encoded_number}/application"
    body = {"application_id": app_id}
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(url, json=body, headers=_headers(auth_id, auth_token))
        if r.status_code == 204:
            logger.info("Vobiz attach {} to app {} -> HTTP 204 (OK)", phone_number, app_id)
            return {"status": "attached"}
        data: dict[str, Any] = r.json()
        logger.info("Vobiz attach {} to app {} -> HTTP {} {}", phone_number, app_id, r.status_code, data)
        if r.status_code >= 400:
            raise RuntimeError(f"Vobiz attach_number failed: {data}")
        return data


async def detach_number_from_application(
    auth_id: str,
    auth_token: str,
    app_id: str,
    phone_number: str,
) -> dict[str, Any]:
    """Detach a phone number from a Vobiz Application.

    Endpoint: DELETE /Account/{auth_id}/numbers/{url_encoded_number}/application
    """
    from urllib.parse import quote

    encoded_number = quote(phone_number, safe="")
    url = f"{_VOBIZ_API_BASE}/Account/{auth_id}/numbers/{encoded_number}/application"
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.delete(url, headers=_headers(auth_id, auth_token))
        data: dict[str, Any] = r.json()
        logger.info("Vobiz detach {} from app {} -> HTTP {} {}", phone_number, app_id, r.status_code, data)
        if r.status_code >= 400:
            raise RuntimeError(f"Vobiz detach_number failed: {data}")
        return data


async def list_phone_numbers(auth_id: str, auth_token: str) -> list[dict[str, Any]]:
    """List all phone numbers (DIDs) on this Vobiz account."""
    url = f"{_VOBIZ_API_BASE}/Account/{auth_id}/PhoneNumber/"
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(url, headers=_headers(auth_id, auth_token))
        data: dict[str, Any] = r.json()
        if r.status_code >= 400:
            raise RuntimeError(f"Vobiz list_phone_numbers failed: {data}")
        return data.get("phone_numbers") or data.get("data") or [data] or []
