"""operator-chosen settings, starting with the watch-only feed's on or off"""

import sqlalchemy as sa
from alembic import op

revision = "0012_operator_settings"
down_revision = "0011_daily_equity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "operator_settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.String(256), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.String(32), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("operator_settings")
