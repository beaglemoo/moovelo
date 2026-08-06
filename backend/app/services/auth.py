"""Password hashing and DB-backed session management."""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from pwdlib import PasswordHash
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Session, User

password_hasher = PasswordHash.recommended()

SESSION_COOKIE = "bikegps_session"


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return password_hasher.verify(password, password_hash)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def user_count(db: AsyncSession) -> int:
    return (await db.execute(select(func.count(User.id)))).scalar_one()


async def get_user_by_email(db: AsyncSession, email: str) -> User | None:
    normalized = email.strip().lower()
    return (await db.execute(select(User).where(User.email == normalized))).scalar_one_or_none()


async def create_user(db: AsyncSession, email: str, password: str, is_admin: bool) -> User:
    user = User(
        email=email.strip().lower(),
        password_hash=hash_password(password),
        is_admin=is_admin,
    )
    db.add(user)
    await db.flush()
    return user


async def create_session(db: AsyncSession, user: User) -> str:
    """Create a session and return the raw token for the cookie."""
    token = secrets.token_urlsafe(32)
    db.add(
        Session(
            token_hash=_token_hash(token),
            user_id=user.id,
            expires_at=datetime.now(UTC) + timedelta(days=settings.session_ttl_days),
        )
    )
    await db.flush()
    return token


async def get_session_user(db: AsyncSession, token: str) -> User | None:
    now = datetime.now(UTC)
    result = await db.execute(
        select(User, Session)
        .join(Session, Session.user_id == User.id)
        .where(Session.token_hash == _token_hash(token), Session.expires_at > now)
    )
    row = result.first()
    if row is None:
        return None
    user: User = row[0]
    session: Session = row[1]
    # Sliding expiry: refresh when less than half the TTL remains.
    if session.expires_at - now < timedelta(days=settings.session_ttl_days / 2):
        session.expires_at = now + timedelta(days=settings.session_ttl_days)
    return user


async def delete_session(db: AsyncSession, token: str) -> None:
    await db.execute(delete(Session).where(Session.token_hash == _token_hash(token)))
