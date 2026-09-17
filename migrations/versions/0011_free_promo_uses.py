"""Bind each user's free promo to one flow; preserve already issued access."""

import sqlalchemy as sa
from alembic import op

revision = "0011_free_promo_uses"
down_revision = "0010_payment_receipts"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "free_promo_uses",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column(
            "code", sa.String(64), sa.ForeignKey("promo_codes.code"), primary_key=True
        ),
        sa.Column("flow_id", sa.Integer(), sa.ForeignKey("flows.id"), nullable=False),
        sa.Column(
            "payment_id", sa.Integer(), sa.ForeignKey("payments.id"), nullable=False
        ),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=False),
    )
    # Historical payments have no promo code. Attribute only to the latest
    # still-recorded selection at payment time, inside the code's validity dates.
    # The first redemption binds the code; later paid access is NOT revoked.
    op.execute("""
        INSERT INTO free_promo_uses (user_id, code, flow_id, payment_id, used_at)
        SELECT DISTINCT ON (up.user_id, up.code)
            up.user_id, up.code, p.flow_id, p.id, p.paid_at
        FROM user_promos up
        JOIN promo_codes pc ON pc.code = up.code AND pc.kind = 'free'
        JOIN payments p ON p.user_id = up.user_id AND p.status = 'paid'
            AND p.provider = 'promo' AND p.amount_rub = 0
            AND p.flow_id IS NOT NULL AND p.paid_at >= up.applied_at
        WHERE (pc.starts_at IS NULL OR p.paid_at >= pc.starts_at)
          AND (pc.ends_at IS NULL OR p.paid_at <= pc.ends_at)
          AND NOT EXISTS (
              SELECT 1 FROM user_promos newer
              WHERE newer.user_id = up.user_id AND newer.applied_at <= p.paid_at
                AND (newer.applied_at, newer.code) > (up.applied_at, up.code)
          )
        ORDER BY up.user_id, up.code, p.paid_at, p.id
    """)


def downgrade():
    op.drop_table("free_promo_uses")
