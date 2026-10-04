"""enable row level security (Supabase)

Supabase exposes every table in the `public` schema through its auto-generated REST API,
reachable with the project's public "anon" key. With RLS on and NO policies, that API can
read/write nothing. Our app connects directly as the table owner, which bypasses RLS, so
the app is unaffected. Postgres only; a no-op on SQLite (local dev and tests).

Revision ID: b3f1a2c4d5e6
Revises: 95b7c19827fa
Create Date: 2026-10-04 20:00:00

"""

from collections.abc import Sequence

from alembic import op

revision: str = "b3f1a2c4d5e6"
down_revision: str | Sequence[str] | None = "95b7c19827fa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ["leads", "runs", "steps", "research_cache", "emails", "alembic_version"]


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for t in TABLES:
        op.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for t in TABLES:
        op.execute(f"ALTER TABLE {t} DISABLE ROW LEVEL SECURITY")
