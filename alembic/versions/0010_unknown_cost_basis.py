"""a position with no known cost basis stores NULL instead of 0"""

import sqlalchemy as sa
from alembic import op

revision = "0010_unknown_cost_basis"
down_revision = "0009_watchlist"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("positions_snapshot") as table:
        table.alter_column("average_price", existing_type=sa.Numeric(38, 18), nullable=True)
    # Venue adapters recorded 0 for "unknown"; no venue reports a real cost of 0.
    op.execute("UPDATE positions_snapshot SET average_price = NULL WHERE average_price = 0")


def downgrade() -> None:
    op.execute("UPDATE positions_snapshot SET average_price = 0 WHERE average_price IS NULL")
    with op.batch_alter_table("positions_snapshot") as table:
        table.alter_column("average_price", existing_type=sa.Numeric(38, 18), nullable=False)
