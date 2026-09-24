"""group portfolio snapshot rows so restart recovery can load the latest baseline"""

from collections import defaultdict
from datetime import UTC, datetime
from uuid import uuid4

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
    _backfill_legacy_snapshots()


def _backfill_legacy_snapshots() -> None:
    """Preserve the newest pre-batch portfolio state during the upgrade.

    Before revision 0004, each position and balance row was written without a
    batch identifier. Reconstruct one complete baseline per source by keeping
    the newest row for each symbol/asset. This is intentionally a one-time
    migration backfill; new writes always create an explicit batch.
    """

    bind = op.get_bind()
    positions = bind.execute(
        sa.text(
            """
            SELECT snapshot_id, source, symbol, as_of
            FROM positions_snapshot
            WHERE batch_id IS NULL
            ORDER BY source, as_of DESC, snapshot_id DESC
            """
        )
    ).mappings()
    balances = bind.execute(
        sa.text(
            """
            SELECT snapshot_id, source, asset, as_of
            FROM balances_snapshot
            WHERE batch_id IS NULL
            ORDER BY source, as_of DESC, snapshot_id DESC
            """
        )
    ).mappings()

    newest_positions: dict[str, dict[str, dict[str, object]]] = defaultdict(dict)
    newest_balances: dict[str, dict[str, dict[str, object]]] = defaultdict(dict)
    for row in positions:
        legacy_row: dict[str, object] = dict(row)
        newest_positions[str(row["source"])].setdefault(str(row["symbol"]), legacy_row)
    for row in balances:
        legacy_row = dict(row)
        newest_balances[str(row["source"])].setdefault(str(row["asset"]), legacy_row)

    for source in sorted(set(newest_positions) | set(newest_balances)):
        rows = [*newest_positions[source].values(), *newest_balances[source].values()]
        recorded_at_values = [row["as_of"] for row in rows if isinstance(row["as_of"], datetime)]
        recorded_at = max(recorded_at_values, default=datetime.now(UTC))
        batch_id = str(uuid4())
        bind.execute(
            sa.text(
                "INSERT INTO portfolio_snapshots (batch_id, source, recorded_at) "
                "VALUES (:batch_id, :source, :recorded_at)"
            ),
            {"batch_id": batch_id, "source": source, "recorded_at": recorded_at},
        )
        for row in newest_positions[source].values():
            bind.execute(
                sa.text(
                    "UPDATE positions_snapshot SET batch_id = :batch_id "
                    "WHERE snapshot_id = :snapshot_id"
                ),
                {"batch_id": batch_id, "snapshot_id": row["snapshot_id"]},
            )
        for row in newest_balances[source].values():
            bind.execute(
                sa.text(
                    "UPDATE balances_snapshot SET batch_id = :batch_id "
                    "WHERE snapshot_id = :snapshot_id"
                ),
                {"batch_id": batch_id, "snapshot_id": row["snapshot_id"]},
            )


def downgrade() -> None:
    for table in ("balances_snapshot", "positions_snapshot"):
        op.drop_index(f"ix_{table}_batch_id", table_name=table)
        op.drop_column(table, "batch_id")
    op.drop_index("ix_portfolio_snapshots_source_recorded_at", table_name="portfolio_snapshots")
    op.drop_table("portfolio_snapshots")
