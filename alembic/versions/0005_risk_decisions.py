"""persist every risk decision so each order links to the approval it cites"""

import sqlalchemy as sa
from alembic import op

revision = "0005_risk_decisions"
down_revision = "0004_portfolio_snapshot_batches"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "risk_decisions",
        sa.Column("approval_id", sa.Uuid(), primary_key=True),
        sa.Column("signal_id", sa.Uuid(), nullable=False),
        sa.Column("approved", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("failed_gate", sa.String(64), nullable=True),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_risk_decisions_signal_id", "risk_decisions", ["signal_id"])


def downgrade() -> None:
    op.drop_index("ix_risk_decisions_signal_id", table_name="risk_decisions")
    op.drop_table("risk_decisions")
