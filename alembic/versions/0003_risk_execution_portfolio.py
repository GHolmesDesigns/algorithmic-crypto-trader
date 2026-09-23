"""add portfolio snapshots and reconciliation discrepancies"""

import sqlalchemy as sa
from alembic import op

revision = "0003_risk_execution_portfolio"
down_revision = "0002_market_candles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for column in (
        sa.Column("symbol", sa.String(32), nullable=True),
        sa.Column("side", sa.String(8), nullable=True),
        sa.Column("order_type", sa.String(8), nullable=True),
        sa.Column("quantity", sa.Numeric(38, 18), nullable=True),
        sa.Column("limit_price", sa.Numeric(38, 18), nullable=True),
        sa.Column("correlation_id", sa.Uuid(), nullable=True),
    ):
        op.add_column("orders", column)
    op.create_table(
        "positions_snapshot",
        sa.Column("snapshot_id", sa.Uuid(), primary_key=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("quantity", sa.Numeric(38, 18), nullable=False),
        sa.Column("average_price", sa.Numeric(38, 18), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
    )
    op.create_table(
        "balances_snapshot",
        sa.Column("snapshot_id", sa.Uuid(), primary_key=True),
        sa.Column("asset", sa.String(32), nullable=False),
        sa.Column("available", sa.Numeric(38, 18), nullable=False),
        sa.Column("hold", sa.Numeric(38, 18), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
    )
    op.create_table(
        "equity_curve",
        sa.Column("snapshot_id", sa.Uuid(), primary_key=True),
        sa.Column("equity", sa.Numeric(38, 18), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
    )
    op.create_table(
        "discrepancies",
        sa.Column("discrepancy_id", sa.Uuid(), primary_key=True),
        sa.Column("entity_type", sa.String(32), nullable=False),
        sa.Column("entity_key", sa.String(128), nullable=False),
        sa.Column("local_payload", sa.JSON(), nullable=False),
        sa.Column("broker_payload", sa.JSON(), nullable=False),
        sa.Column("safety_action", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    for table in ("discrepancies", "equity_curve", "balances_snapshot", "positions_snapshot"):
        op.drop_table(table)
    for name in ("correlation_id", "limit_price", "quantity", "order_type", "side", "symbol"):
        op.drop_column("orders", name)
