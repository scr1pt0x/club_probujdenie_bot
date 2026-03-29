"""add needs_review payment status

Revision ID: 0006_payment_status_needs_review
Revises: 0005_payments_external_id_nullable
Create Date: 2026-03-23
"""
from alembic import op


revision = "0006_payment_status_needs_review"
down_revision = "0005_payments_external_id_nullable"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE payment_status ADD VALUE IF NOT EXISTS 'needs_review'")


def downgrade() -> None:
    # PostgreSQL does not support removing enum values safely in-place.
    pass
