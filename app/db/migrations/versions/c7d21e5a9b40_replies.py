"""replies

Revision ID: c7d21e5a9b40
Revises: f028981b3077
Create Date: 2026-10-11 01:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7d21e5a9b40"
down_revision: str | Sequence[str] | None = "f028981b3077"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "replies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email_id", sa.Uuid(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("quote", sa.Text(), nullable=True),
        sa.Column("needs_human", sa.Boolean(), nullable=False),
        sa.Column("decided_by_code", sa.Boolean(), nullable=False),
        sa.Column("suppressed", sa.Boolean(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("created_by", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["email_id"], ["emails.id"], name="fk_replies_email_id"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_replies_email_id", "replies", ["email_id"], unique=False)
    if op.get_bind().dialect.name == "postgresql":  # like every table: see b3f1a2c4d5e6
        op.execute("ALTER TABLE replies ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_replies_email_id", table_name="replies")
    op.drop_table("replies")
