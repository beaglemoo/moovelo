"""Install-wide assistant endpoint configuration, editable from /admin.

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-09

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Every column is nullable: NULL means "use the environment variable",
    # so an install that never opens the admin page behaves exactly as it
    # did before this migration ran.
    op.create_table(
        "llm_settings",
        sa.Column("id", sa.Boolean(), primary_key=True, server_default="true"),
        sa.Column("base_url", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("api_key", sa.Text(), nullable=True),
        sa.Column("provider_order", sa.Text(), nullable=True),
        sa.Column("provider_sort", sa.String(length=20), nullable=True),
        sa.Column("max_prompt_price", sa.Float(), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("id", name="ck_llm_settings_singleton"),
    )


def downgrade() -> None:
    # Dropping the table returns the install to env-only configuration.
    # Any endpoint, model or routing choice made through the admin page is
    # lost, so an operator downgrading past this revision must set the
    # equivalent LLM_* environment variables or the assistant goes quiet -
    # which is the safe direction to fail.
    op.drop_table("llm_settings")
