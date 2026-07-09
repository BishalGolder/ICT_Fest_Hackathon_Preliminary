"""Authentication: password hashing, JWT issue/verify, request dependencies."""
import hashlib
import hmac
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, Request
from sqlalchemy.orm import Session

from .config import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    JWT_ALGORITHM,
    JWT_SECRET,
    REFRESH_TOKEN_EXPIRE_DAYS,
)
from .database import get_db
from .errors import AppError
from .models import User

# Thread safety tracking structures
_auth_lock = threading.Lock()
_revoked_tokens: set[str] = set()

_PBKDF2_ROUNDS = 100_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ROUNDS)
    return f"{salt.hex()}:{dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, dk_hex = stored.split(":")
    except ValueError:
        return False

    dk = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode(),
        bytes.fromhex(salt_hex),
        _PBKDF2_ROUNDS,
    )
    return hmac.compare_digest(bytes.fromhex(dk_hex), dk)


def create_access_token(user) -> str:
    lifetime = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "org": user.org_id,
        "role": user.role,
        "jti": str(uuid.uuid4()),
        "iat": now,
        "exp": now + lifetime,
        "type": "access",
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def create_refresh_token(user) -> str:
    lifetime = timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "org": user.org_id,
        "role": user.role,
        "jti": str(uuid.uuid4()),
        "iat": now,
        "exp": now + lifetime,
        "type": "refresh",
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        with _auth_lock:
            if payload.get("jti") in _revoked_tokens:
                raise AppError(401, "UNAUTHORIZED", "Token has been revoked")
        return payload
    except jwt.PyJWTError:
        raise AppError(401, "UNAUTHORIZED", "Invalid or expired token")


def consume_refresh_token(token: str) -> dict:
    """
    Atomically decode, validate, and revoke a refresh token.
    This prevents concurrent reuse of the same refresh token.
    """
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        raise AppError(401, "UNAUTHORIZED", "Invalid or expired token")

    if payload.get("type") != "refresh":
        raise AppError(401, "UNAUTHORIZED", "Wrong token type")

    jti = payload.get("jti")
    with _auth_lock:
        if jti in _revoked_tokens:
            raise AppError(401, "UNAUTHORIZED", "Token has been revoked")
        _revoked_tokens.add(jti)

    return payload


def revoke_access_token(payload: dict) -> None:
    if "jti" in payload:
        with _auth_lock:
            _revoked_tokens.add(payload["jti"])


def get_token_payload(request: Request) -> dict:
    header = request.headers.get("Authorization")
    if not header or not header.startswith("Bearer "):
        raise AppError(401, "UNAUTHORIZED", "Missing bearer token")

    token = header[len("Bearer "):].strip()
    payload = decode_token(token)

    if payload.get("type") != "access":
        raise AppError(401, "UNAUTHORIZED", "Wrong token type")

    return payload


def get_current_user(
    payload: dict = Depends(get_token_payload),
    db: Session = Depends(get_db),
) -> User:
    user = db.query(User).filter(User.id == int(payload["sub"])).first()

    if user is None:
        raise AppError(401, "UNAUTHORIZED", "User not found")

    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise AppError(403, "FORBIDDEN", "Admin role required")

    return user