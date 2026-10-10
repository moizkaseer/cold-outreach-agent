"""Create an API key for a team. The key is printed ONCE; only its hash is stored.

    uv run python -m scripts.create_api_key "Acme sales"
    uv run python -m scripts.create_api_key --list
    uv run python -m scripts.create_api_key --revoke oa_AbCdEfG    # the prefix from --list

Uses DATABASE_URL from the environment / .env (no API keys needed).
"""

import argparse
import asyncio

from sqlalchemy import select

from app.api.auth import create_api_key
from app.config import InfraSettings
from app.db.models import ApiKey, utcnow
from app.db.session import make_engine, make_sessionmaker


async def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("name", nargs="?", help="team or owner name; recorded as the reviewer")
    p.add_argument("--list", action="store_true")
    p.add_argument("--revoke", metavar="PREFIX")
    args = p.parse_args()

    engine = make_engine(InfraSettings().database_url)
    sessions = make_sessionmaker(engine)
    try:
        if args.list:
            async with sessions() as s:
                for k in await s.scalars(select(ApiKey).order_by(ApiKey.created_at)):
                    state = "revoked" if k.revoked_at else "active"
                    print(f"{k.prefix}...  {state:8}  {k.created_at:%Y-%m-%d}  {k.name}")
        elif args.revoke:
            async with sessions() as s:
                keys = list(await s.scalars(select(ApiKey).where(ApiKey.prefix == args.revoke)))
                if len(keys) != 1:
                    raise SystemExit(f"{len(keys)} keys match that prefix; nothing revoked")
                keys[0].revoked_at = utcnow()
                await s.commit()
                print(f"revoked {keys[0].prefix}... ({keys[0].name})")
        elif args.name:
            _, raw = await create_api_key(sessions, args.name)
            print(f"API key for {args.name!r} (shown once, store it now):\n\n  {raw}\n")
        else:
            p.error("give a team name, --list or --revoke PREFIX")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
