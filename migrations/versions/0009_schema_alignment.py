"""Align legacy nullability and lookup indexes without removing archived data."""

from alembic import op

revision = "0009_schema_alignment"
down_revision = "0008_user_access_suspended"
branch_labels = None
depends_on = None


def upgrade():
    # Unknown flow types must not silently become paid flows. Abort the entire
    # migration if invalid legacy rows need an operator's explicit decision.
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM flows WHERE is_free IS NULL)
               OR EXISTS (SELECT 1 FROM users WHERE is_admin IS NULL) THEN
                RAISE EXCEPTION 'Resolve NULL flow/user flags before migration';
            END IF;
        END $$;
    """)
    op.alter_column("flows", "is_free", nullable=False)
    op.alter_column("users", "is_admin", nullable=False)
    for table, column in (
        ("memberships", "user_id"),
        ("memberships", "flow_id"),
        ("payments", "user_id"),
    ):
        op.execute(
            f"CREATE INDEX IF NOT EXISTS ix_{table}_{column} ON {table} ({column})"
        )


def downgrade():
    op.alter_column("flows", "is_free", nullable=True)
    op.alter_column("users", "is_admin", nullable=True)
    # Retain harmless indexes: they may have predated this migration on a server.
