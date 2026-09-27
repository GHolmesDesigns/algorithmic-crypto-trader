"""administrator evidence records for the soak and readiness criterion tracker"""

import sqlalchemy as sa
from alembic import op

revision = "0007_evidence_records"
down_revision = "0006_history_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "evidence_records",
        sa.Column("evidence_id", sa.Uuid(), primary_key=True),
        sa.Column("criterion", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(8), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("recorded_by", sa.String(32), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_evidence_records_criterion_recorded_at",
        "evidence_records",
        ["criterion", "recorded_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_evidence_records_criterion_recorded_at", table_name="evidence_records")
    op.drop_table("evidence_records")
