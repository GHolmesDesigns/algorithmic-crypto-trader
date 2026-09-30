"""equity snapshots can be partial, and hold the last-known price behind each valuation"""

import sqlalchemy as sa
from alembic import op

revision = "0011_daily_equity"
down_revision = "0010_unknown_cost_basis"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("equity_curve") as table:
        table.alter_column("equity", existing_type=sa.Numeric(38, 18), nullable=True)
        table.add_column(
            sa.Column("partial", sa.Boolean(), nullable=False, server_default=sa.false())
        )
    op.create_table(
        "equity_snapshot_holdings",
        sa.Column("holding_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "snapshot_id", sa.Uuid(), sa.ForeignKey("equity_curve.snapshot_id"), nullable=False
        ),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("basis", sa.String(16), nullable=False),
        sa.Column("quantity", sa.Numeric(38, 18), nullable=False),
        sa.Column("price", sa.Numeric(38, 18), nullable=True),
        sa.Column("price_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("price_age_seconds", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_equity_snapshot_holdings_snapshot", "equity_snapshot_holdings", ["snapshot_id"]
    )
    op.create_table(
        "last_known_prices",
        sa.Column("symbol", sa.String(32), primary_key=True),
        sa.Column("bid", sa.Numeric(38, 18), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("last_known_prices")
    op.drop_index("ix_equity_snapshot_holdings_snapshot", table_name="equity_snapshot_holdings")
    op.drop_table("equity_snapshot_holdings")
    # A partial snapshot has no value to keep.
    op.execute("DELETE FROM equity_curve WHERE equity IS NULL")
    with op.batch_alter_table("equity_curve") as table:
        table.drop_column("partial")
        table.alter_column("equity", existing_type=sa.Numeric(38, 18), nullable=False)
