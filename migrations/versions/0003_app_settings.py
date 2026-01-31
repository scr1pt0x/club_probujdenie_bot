"""add app_settings

Revision ID: 0003_app_settings
Revises: 0002_user_tg_id_bigint
Create Date: 2026-01-31

"""
from alembic import op
import sqlalchemy as sa


revision = "0003_app_settings"
down_revision = "0002_user_tg_id_bigint"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(length=64), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("app_settings")
