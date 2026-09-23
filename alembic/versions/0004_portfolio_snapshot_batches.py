"""group portfolio snapshot rows so restart recovery can load the latest baseline"""

import sqlalchemy as sa
from alembic import op

revision = "0004_portfolio_snapshot_batches"
down_revision = "0003_risk_execution_portfolio"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "portfolio_snapshots",
        sa.Column("batch_id", sa.Uuid(), primary_key=True),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_portfolio_snapshots_source_recorded_at",
        "portfolio_snapshots",
        ["source", "recorded_at"],
    )
    for table in ("positions_snapshot", "balances_snapshot"):
        op.add_column(table, sa.Column("batch_id", sa.Uuid(), nullable=True))
        op.create_index(f"ix_{table}_batch_id", table, ["batch_id"])


def downgrade() -> None:
    for table in ("balances_snapshot", "positions_snapshot"):
        op.drop_index(f"ix_{table}_batch_id", table_name=table)
        op.drop_column(table, "batch_id")
    op.drop_index("ix_portfolio_snapshots_source_recorded_at", table_name="portfolio_snapshots")
    op.drop_table("portfolio_snapshots")
