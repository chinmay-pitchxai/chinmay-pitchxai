"""JWT-based authentication for production use."""

from __future__ import annotations

import os
import secrets
import time
import base64
import json
import hashlib
import hmac
from pathlib import Path
from typing import Optional

import jwt
import bcrypt
from fastapi import Request, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from loguru import logger
from slowapi import Limiter
from slowapi.util import get_remote_address


def _resolve_jwt_secret() -> str:
    """Prefer ``JWT_SECRET_KEY`` env; otherwise persist ``backend/data/.jwt_secret``.
    Restarting systemd/uvicorn used to regenerate a secret from `time`+pid, invalidating every
    Bearer token (`Invalid or expired token` on `/api/manual/call`, etc.)."""
    raw = os.getenv("JWT_SECRET_KEY", "").strip()
    if raw:
        return raw

    secrets_path = Path(__file__).resolve().parent.parent / "data" / ".jwt_secret"
    secrets_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if secrets_path.is_file():
            txt = secrets_path.read_text(encoding="utf-8").strip()
            if txt:
                return txt
        new_secret = secrets.token_hex(32)
        fd = os.open(secrets_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, (new_secret + "\n").encode("utf-8"))
        finally:
            os.close(fd)
        logger.warning(
            "JWT_SECRET_KEY unset — wrote persistent secret {} (restart-safe; "
            "set JWT_SECRET_KEY in .env if you replicate servers).",
            secrets_path,
        )
        return new_secret
    except FileExistsError:
        txt = secrets_path.read_text(encoding="utf-8").strip()
        if txt:
            return txt
    except OSError as e:
        ephemeral = hashlib.sha256(f"VERN_JWT_EPHEMERAL-{time.time()}".encode()).hexdigest()
        logger.error(
            "JWT_SECRET_KEY unset and cannot write {}; using ephemeral JWT secret (tokens break on restart): {}",
            secrets_path,
            e,
        )
        return ephemeral

    ephemeral = hashlib.sha256(f"VERN_JWT_EPHEMERAL-{time.time()}".encode()).hexdigest()
    logger.error(
        ".jwt_secret empty after race — using ephemeral JWT secret; set JWT_SECRET_KEY or retry."
    )
    return ephemeral


SECRET_KEY = _resolve_jwt_secret()

ALGORITHM = "HS256"
TOKEN_EXPIRY_HOURS = 24

security = HTTPBearer(auto_error=False)

# Roles that map to independent agent sandboxes.
_SANDBOX_ROLES = frozenset({"sales_1"})
# Admin / master login can flip between sandboxes.
_CONSOLE_ADMIN_ROLES = frozenset({"sales_1"})
_CONSOLE_SWITCHABLE_ROLES = frozenset({"sales_1"})
_CONSOLE_LOCKED_ROLES = frozenset()  # no role is hard-locked; admins can switch

# Simple user store
_VALID_USERS = {
    "test@technopolis.com": {
        "password_hash": b"$2b$12$LJ3m4ys3Lk0TSwHCpNqrBeXkOTY3pKL8C.MhE.yOFPq.HO/KMQiqK",
        "role": "sales_1",
    },
    "realestate@gmail.com": {
        "password_hash": b"$2b$12$yh.1DNiYFSjXY7T6EAeK/OvRMoK/XPzEeV2ZLKdjzfv6fECYAdK6q",
        "role": "sales_1",
    },
}



def dashboard_role_for_token(email: str | None, jwt_role: str | None) -> str:
    """Canonical console dataset role for this login.

    Sandbox logins (sales_1) stay in their own sandbox.
    Admin logins default to sales_1 but the UI may switch.
    """
    from core.state import normalize_console_role

    r = normalize_console_role(jwt_role)
    if r in _SANDBOX_ROLES:
        return r
    # Admin / unknown → start at sales_1 so the toggle is meaningful.
    return "sales_1"


def console_session_meta(email: str | None, jwt_role: str | None) -> dict[str, object]:
    """Payload for ``GET /api/me`` — single source of truth for the operator UI."""
    from core.state import normalize_console_role

    r = normalize_console_role(jwt_role)
    is_admin = r in _CONSOLE_ADMIN_ROLES
    return {
        "email": (email or "").strip().lower(),
        "jwt_role": r,
        "dashboard_role": dashboard_role_for_token(email, jwt_role),
        "locked": not is_admin,
        "can_switch_roles": is_admin,
        "available_roles": sorted(_SANDBOX_ROLES),
    }


def jwt_payload_from_request(request: Request) -> dict | None:
    """Decode Bearer JWT or ``access_token`` / ``token`` query param."""
    auth = (request.headers.get("Authorization") or "").strip()
    if auth.startswith("Bearer "):
        payload = _decode_jwt(auth[7:])
        if payload:
            return payload
    for key in ("access_token", "token"):
        raw = (request.query_params.get(key) or "").strip()
        if raw:
            payload = _decode_jwt(raw)
            if payload:
                return payload
    return None


def console_role_from_request(request: Request, *, default: str = "sales_1") -> str:
    """Resolve campaign/console role from JWT."""
    from core.state import normalize_console_role

    payload = jwt_payload_from_request(request)
    if payload:
        return dashboard_role_for_token(
            payload.get("email"),
            payload.get("role"),
        )
    return normalize_console_role(default)


limiter = Limiter(key_func=get_remote_address)


def _encode_jwt(payload: dict) -> str:
    """Encode payload as a JWT using PyJWT."""
    payload["exp"] = int(time.time()) + (TOKEN_EXPIRY_HOURS * 3600)
    payload["iat"] = int(time.time())
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def _decode_jwt(token: str) -> Optional[dict]:
    """Decode and verify a JWT; fallback to legacy hand-rolled decoder for compatibility."""
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        pass

    # Fallback: legacy hand-rolled HMAC decoder
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None

        header_b64, payload_b64, signature_b64 = parts

        signature_input = f"{header_b64}.{payload_b64}".encode()
        expected_sig = hmac.new(
            SECRET_KEY.encode(), signature_input, hashlib.sha256
        ).digest()

        sig_padded = signature_b64 + "=" * (4 - len(signature_b64) % 4)
        provided_sig = base64.urlsafe_b64decode(sig_padded)

        if not hmac.compare_digest(expected_sig, provided_sig):
            return None

        payload_padded = payload_b64 + "=" * (4 - len(payload_b64) % 4)
        payload_data = json.loads(base64.urlsafe_b64decode(payload_padded))

        if payload_data.get("exp", 0) < time.time():
            return None

        return payload_data
    except Exception:
        return None


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> dict:
    if not credentials:
        raise HTTPException(status_code=401, detail="Not authenticated")

    payload = _decode_jwt(credentials.credentials)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    return {
        "email": payload.get("email"),
        "role": payload.get("role"),
    }


async def get_current_user_optional(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> Optional[dict]:
    """Optional auth — returns None if no token (for public endpoints)."""
    if not credentials:
        return None
    payload = _decode_jwt(credentials.credentials)
    if not payload:
        return None
    return {"email": payload.get("email"), "role": payload.get("role")}


def create_token(email: str, role: str) -> dict:
    """Create a JWT token and return it."""
    token = _encode_jwt({"email": email, "role": role})
    return {"token": token, "expires_in": TOKEN_EXPIRY_HOURS * 3600}


def verify_password(email: str, password: str) -> Optional[str]:
    """Verify user credentials and return role, or None if invalid."""
    user = _VALID_USERS.get(email.lower())
    if not user:
        return None
    stored_hash = user["password_hash"]
    if isinstance(stored_hash, str):
        stored_hash = stored_hash.encode()
    if bcrypt.checkpw(password.encode(), stored_hash):
        return user["role"]
    return None


def require_role(role: str):
    """Dependency factory that validates the JWT role matches the requested role."""
    async def _require_role(
        credentials: HTTPAuthorizationCredentials = Depends(security),
    ) -> dict:
        if not credentials:
            raise HTTPException(status_code=401, detail="Not authenticated")
        payload = _decode_jwt(credentials.credentials)
        if not payload:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        user_role = payload.get("role")
        if user_role != role:
            raise HTTPException(
                status_code=403, detail=f"Role '{role}' required"
            )
        return {
            "email": payload.get("email"),
            "role": user_role,
        }

    return _require_role
