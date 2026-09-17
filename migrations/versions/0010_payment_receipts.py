"""Durable delivery state for payment confirmations, without historical sends."""

import sqlalchemy as sa
from alembic import op

revision = "0010_payment_receipts"
down_revision = "0009_schema_alignment"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "payment_receipts",
        sa.Column(
            "payment_id", sa.Integer(), sa.ForeignKey("payments.id"), primary_key=True
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
    )


def downgrade():
    op.drop_table("payment_receipts")
