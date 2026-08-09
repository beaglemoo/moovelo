"""Custom costing sliders: per-route override and saved per-user presets.

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-08

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("routes", sa.Column("costing_options", JSONB(), nullable=True))

    op.create_table(
        "custom_presets",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(60), nullable=False),
        sa.Column("options", JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("user_id", "name"),
    )
    op.create_index("ix_custom_presets_user_id", "custom_presets", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_custom_presets_user_id", table_name="custom_presets")
    op.drop_table("custom_presets")
    # preset="custom" only means anything alongside costing_options, which is
    # about to be dropped - leaving it behind would strand rows in a state the
    # app's own invariant forbids (custom <-> options travel together), and
    # older code looks preset up in PRESETS, where "custom" is a KeyError. The
    # bundle is lost either way; falling back to the default named preset is
    # the honest landing, and the route's stored geometry is untouched.
    op.execute("UPDATE routes SET preset = 'road' WHERE preset = 'custom'")
    op.drop_column("routes", "costing_options")
