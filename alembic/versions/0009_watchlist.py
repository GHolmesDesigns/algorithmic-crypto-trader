"""saved watchlist of coins to chart without trading them"""

import sqlalchemy as sa
from alembic import op

revision = "0009_watchlist"
down_revision = "0008_incident_records"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "watchlist",
        sa.Column("symbol", sa.String(32), primary_key=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("added_by", sa.String(32), nullable=False),
        sa.UniqueConstraint("position", name="uq_watchlist_position"),
        sa.CheckConstraint("position >= 0 AND position < 9", name="ck_watchlist_position"),
    )


def downgrade() -> None:
    op.drop_table("watchlist")
