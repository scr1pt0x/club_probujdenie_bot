"""Explicit administrative suspension; never infer it from old expired rows."""

import sqlalchemy as sa
from alembic import op

revision = "0008_user_access_suspended"
down_revision = "0007_user_access_exempt"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "users",
        sa.Column(
            "access_suspended", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )


def downgrade():
    op.drop_column("users", "access_suspended")
