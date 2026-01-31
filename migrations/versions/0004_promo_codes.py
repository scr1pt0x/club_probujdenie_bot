"""promo codes and user promos

Revision ID: 0004_promo_codes
Revises: 0003_app_settings
Create Date: 2026-01-31

"""
from alembic import op
import sqlalchemy as sa


revision = "0004_promo_codes"
down_revision = "0003_app_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "promo_codes",
        sa.Column("code", sa.String(length=64), primary_key=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("value_int", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("max_uses", sa.Integer(), nullable=True),
        sa.Column("used_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "user_promos",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("user_id", "code"),
    )


def downgrade() -> None:
    op.drop_table("user_promos")
    op.drop_table("promo_codes")
