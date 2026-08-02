# Copyright (c) 2026 Asadik Hamed. All rights reserved. See LICENSE.

import hashlib
import hmac
import re
import secrets
import time

from fastapi import HTTPException, Request, status

try:
    from backend.config import ADMIN_KEY, SECRET_KEY, SESSION_TTL_SECONDS
    from backend.models import SessionResponse
except ImportError:
    from config import ADMIN_KEY, SECRET_KEY, SESSION_TTL_SECONDS
    from models import SessionResponse


def _session_signature(session_id: str, csrf_token: str, expires_at: int) -> str:
    payload = f"{session_id}.{expires_at}.{csrf_token}".encode()
    return hmac.new(SECRET_KEY.encode(), payload, hashlib.sha256).hexdigest()


def create_signed_session() -> SessionResponse:
    session_id = secrets.token_urlsafe(24)
    csrf_token = secrets.token_urlsafe(32)
    expires_at = int(time.time() + SESSION_TTL_SECONDS)
    signature = _session_signature(session_id, csrf_token, expires_at)
    signed_session = f"{session_id}.{expires_at}.{csrf_token}.{signature}"
    return SessionResponse(
        session_id=signed_session,
        csrf_token=csrf_token,
        expires_at=expires_at,
    )


def verify_signed_session(signed_session: str, csrf_token: str) -> str:
    if not signed_session or not csrf_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Missing session credentials")

    parts = signed_session.split(".")
    if len(parts) != 4:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid session")

    session_id, expires_raw, token_csrf, signature = parts
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,}", session_id):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid session")
    if not re.fullmatch(r"[A-Fa-f0-9]{64}", signature):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid session")

    try:
        expires_at = int(expires_raw)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid session")

    if expires_at < int(time.time()):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Session expired")

    expected = _session_signature(session_id, token_csrf, expires_at)
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid session")
    if not hmac.compare_digest(token_csrf, csrf_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Invalid CSRF token")
    return session_id


def require_admin_key(request: Request) -> None:
    if not ADMIN_KEY:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Admin functionality not configured")
    provided_key = request.headers.get("X-Admin-Key", "")
    if not provided_key or not hmac.compare_digest(provided_key, ADMIN_KEY):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid admin key")
