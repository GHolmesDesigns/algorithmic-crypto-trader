"""add canonical market candle storage"""

import sqlalchemy as sa
from alembic import op

revision = "0002_market_candles"
down_revision = "0001_audit_spine"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "market_candles",
        sa.Column("candle_id", sa.Uuid(), primary_key=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("interval", sa.String(32), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(38, 18), nullable=False),
        sa.Column("high", sa.Numeric(38, 18), nullable=False),
        sa.Column("low", sa.Numeric(38, 18), nullable=False),
        sa.Column("close", sa.Numeric(38, 18), nullable=False),
        sa.Column("volume", sa.Numeric(38, 18), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("symbol", "interval", "opened_at", name="uq_market_candles_key"),
    )


def downgrade() -> None:
    op.drop_table("market_candles")
