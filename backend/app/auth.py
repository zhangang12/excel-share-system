"""密码哈希 + JWT"""
from datetime import datetime, timedelta, timezone
from typing import Optional
import bcrypt
import jwt
from .config import settings


def hash_password(plain: str) -> str:
    if isinstance(plain, str):
        plain = plain.encode("utf-8")
    return bcrypt.hashpw(plain, bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        if isinstance(plain, str):
            plain = plain.encode("utf-8")
        if isinstance(hashed, str):
            hashed = hashed.encode("utf-8")
        return bcrypt.checkpw(plain, hashed)
    except Exception:
        return False


def create_access_token(subject, extra: Optional[dict] = None) -> str:
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=settings.access_token_expire_minutes
    )
    payload = {"sub": str(subject), "exp": expire}
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


def decode_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
    except jwt.PyJWTError:
        return None
    except Exception:
        return None
