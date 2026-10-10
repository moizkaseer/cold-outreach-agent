"""What every route needs: shared dependencies and "who is calling".

Access model:
- A request with a valid API key acts as that team. Its runs and emails are visible to
  that key only; anyone else gets 404 (not 403, which would confirm the id exists).
- A request with NO key is anonymous. It may create runs only if the deploy allows it
  (the public demo). An anonymous run can be read by whoever holds its id: the id is a
  random UUID, so it works as an unguessable link. Anonymous callers can never review
  or send.
- A request with a WRONG key is always 401. It never falls back to anonymous, so a typo
  can't silently create a run nobody owns.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.auth import find_key
from app.api.limits import Limits
from app.db.models import ApiKey
from app.schemas import Sender


@dataclass
class AppDeps:
    sessions: async_sessionmaker[AsyncSession]
    enqueue: Callable[[uuid.UUID, int], Awaitable[bool]]
    sender: Sender | None  # server default (sender.json); requests may bring their own
    limits: Limits = Limits()  # guarded by default; tests pass their own
    poll_seconds: float = 1.0
    allow_anonymous: bool = False  # the public demo turns this on
    cors_origins: tuple[str, ...] = ()


def get_deps(request: Request) -> AppDeps:
    return request.app.state.deps


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(401, detail, headers={"WWW-Authenticate": "Bearer"})


async def optional_key(request: Request) -> ApiKey | None:
    header = request.headers.get("authorization")
    if header is None:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise _unauthorized("use 'Authorization: Bearer <api key>'")
    key = await find_key(get_deps(request).sessions, token.strip())
    if key is None:
        raise _unauthorized("invalid or revoked API key")
    return key


async def required_key(key: Annotated[ApiKey | None, Depends(optional_key)]) -> ApiKey:
    if key is None:
        raise _unauthorized("API key required")
    return key


Caller = Annotated[ApiKey | None, Depends(optional_key)]  # a team, or anonymous
Team = Annotated[ApiKey, Depends(required_key)]
