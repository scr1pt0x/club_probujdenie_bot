"""add protected complimentary access

Revision ID: 0007_user_access_exempt
Revises: 0006_payment_status_needs_review
Create Date: 2026-08-20
"""

import sqlalchemy as sa
from alembic import op

revision = "0007_user_access_exempt"
down_revision = "0006_payment_status_needs_review"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "access_exempt",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    # This legacy text was previously concatenated with an internal rejection
    # reason. The current UI calculates and displays the actual deadline.
    op.execute("DELETE FROM message_templates WHERE key = 'pay_later_unavailable'")


def downgrade() -> None:
    op.drop_column("users", "access_exempt")
