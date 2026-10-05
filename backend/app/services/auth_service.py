"""Password hashing (bcrypt) and JWT (HS256) tokens."""
from __future__ import annotations

from datetime import timedelta

import bcrypt
import jwt

from app.config import get_settings
from app.utils.timeutil import utcnow

ROLES = ("ADMIN", "DISPATCHER", "VIEWER")


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


def create_token(username: str, role: str) -> str:
    s = get_settings()
    now = utcnow()
    payload = {"sub": username, "role": role, "iat": now, "exp": now + timedelta(minutes=s.jwt_expire_minutes)}
    return jwt.encode(payload, s.resolved_jwt_secret(), algorithm="HS256")


def decode_token(token: str) -> dict:
    return jwt.decode(token, get_settings().resolved_jwt_secret(), algorithms=["HS256"])
