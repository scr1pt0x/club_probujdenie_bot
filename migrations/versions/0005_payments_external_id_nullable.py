"""payments external_id nullable

Revision ID: 0005_payments_external_id_nullable
Revises: 0004_promo_codes
Create Date: 2026-02-01

"""

import sqlalchemy as sa
from alembic import op

revision = "0005_payments_external_id_nullable"
down_revision = "0004_promo_codes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "payments",
        "external_id",
        existing_type=sa.String(length=128),
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "payments",
        "external_id",
        existing_type=sa.String(length=128),
        nullable=False,
    )
