"""API keys: how a sales team proves who it is.

    Authorization: Bearer oa_...

A key is random (256 bits), shown once when created, and only its SHA-256 hash is stored,
so a database leak doesn't leak working keys. A fast hash is fine here: slow hashes
(bcrypt, argon2) exist to protect low-entropy passwords from guessing, and a random key
can't be guessed. Create one with `uv run python -m scripts.create_api_key "Team name"`.
"""

from __future__ import annotations

import hashlib
import secrets

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import ApiKey

KEY_PREFIX = "oa_"


def generate_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


async def create_api_key(
    sessions: async_sessionmaker[AsyncSession], name: str
) -> tuple[ApiKey, str]:
    """Returns the stored row and the raw key. The raw key can't be recovered later."""
    raw = generate_key()
    row = ApiKey(name=name, key_hash=hash_key(raw), prefix=raw[:10])
    async with sessions() as s:
        s.add(row)
        await s.commit()
    return row, raw


async def find_key(sessions: async_sessionmaker[AsyncSession], raw: str) -> ApiKey | None:
    async with sessions() as s:
        return (
            await s.execute(
                select(ApiKey).where(ApiKey.key_hash == hash_key(raw), ApiKey.revoked_at.is_(None))
            )
        ).scalar_one_or_none()
