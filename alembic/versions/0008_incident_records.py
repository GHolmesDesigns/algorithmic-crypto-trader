"""administrator-opened and -closed incident records for the soak digest"""

import sqlalchemy as sa
from alembic import op

revision = "0008_incident_records"
down_revision = "0007_evidence_records"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "incident_records",
        sa.Column("incident_id", sa.Uuid(), primary_key=True),
        sa.Column("cause", sa.Text(), nullable=False),
        sa.Column("opened_by", sa.String(32), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_note", sa.Text(), nullable=True),
        sa.Column("closed_by", sa.String(32), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_incident_records_opened_at", "incident_records", ["opened_at"])


def downgrade() -> None:
    op.drop_index("ix_incident_records_opened_at", table_name="incident_records")
    op.drop_table("incident_records")
