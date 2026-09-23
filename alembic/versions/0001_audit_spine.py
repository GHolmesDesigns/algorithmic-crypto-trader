"""create initial audit spine"""

import sqlalchemy as sa
from alembic import op

revision = "0001_audit_spine"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_notes",
        sa.Column("note_id", sa.Uuid(), primary_key=True),
        sa.Column("category", sa.String(64), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "signals",
        sa.Column("signal_id", sa.Uuid(), primary_key=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("strategy_version", sa.String(128), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("quantity", sa.Numeric(38, 18), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "orders",
        sa.Column("order_id", sa.Uuid(), primary_key=True),
        sa.Column("signal_id", sa.Uuid(), nullable=False),
        sa.Column("client_order_id", sa.Uuid(), nullable=False),
        sa.Column("strategy_version", sa.String(128), nullable=False),
        sa.Column("risk_approval_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("client_order_id", name="uq_orders_client_order_id"),
    )
    op.create_table(
        "fills",
        sa.Column("fill_id", sa.Uuid(), primary_key=True),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("broker_fill_id", sa.String(128), nullable=False),
        sa.Column("quantity", sa.Numeric(38, 18), nullable=False),
        sa.Column("price", sa.Numeric(38, 18), nullable=False),
        sa.Column("fee", sa.Numeric(38, 18), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("broker_fill_id", name="uq_fills_broker_fill_id"),
    )
    op.create_table(
        "system_events",
        sa.Column("event_id", sa.Uuid(), primary_key=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    for table in ("system_events", "fills", "orders", "signals", "audit_notes"):
        op.drop_table(table)
