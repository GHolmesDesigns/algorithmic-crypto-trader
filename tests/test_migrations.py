import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, DateTime, MetaData, String, Table, create_engine


def _load_revision():
    path = Path(__file__).parents[1] / "alembic" / "versions" / "0004_portfolio_snapshot_batches.py"
    spec = importlib.util.spec_from_file_location("revision_0004", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_snapshot_batch_migration_backfills_newest_legacy_rows() -> None:
    engine = create_engine("sqlite+pysqlite://", future=True)
    metadata = MetaData()
    positions = Table(
        "positions_snapshot",
        metadata,
        Column("snapshot_id", String(36), primary_key=True),
        Column("symbol", String(32), nullable=False),
        Column("quantity", String(32), nullable=False),
        Column("average_price", String(32), nullable=False),
        Column("as_of", DateTime(timezone=True), nullable=False),
        Column("source", String(64), nullable=False),
    )
    balances = Table(
        "balances_snapshot",
        metadata,
        Column("snapshot_id", String(36), primary_key=True),
        Column("asset", String(32), nullable=False),
        Column("available", String(32), nullable=False),
        Column("hold", String(32), nullable=False),
        Column("as_of", DateTime(timezone=True), nullable=False),
        Column("source", String(64), nullable=False),
    )
    metadata.create_all(engine)
    newest = datetime(2026, 9, 24, 12, tzinfo=UTC)
    older = newest - timedelta(hours=1)
    with engine.begin() as connection:
        connection.execute(
            positions.insert(),
            [
                {
                    "snapshot_id": "old-btc",
                    "symbol": "BTC-USD",
                    "quantity": "1",
                    "average_price": "60000",
                    "as_of": older,
                    "source": "broker",
                },
                {
                    "snapshot_id": "new-btc",
                    "symbol": "BTC-USD",
                    "quantity": "2",
                    "average_price": "61000",
                    "as_of": newest,
                    "source": "broker",
                },
            ],
        )
        connection.execute(
            balances.insert(),
            {
                "snapshot_id": "new-usd",
                "asset": "USD",
                "available": "100000",
                "hold": "0",
                "as_of": newest,
                "source": "broker",
            },
        )
        context = MigrationContext.configure(connection)
        operations = Operations(context)
        revision = _load_revision()
        revision.op = operations
        revision.upgrade()

        result = connection.exec_driver_sql(
            "SELECT p.snapshot_id, p.batch_id FROM positions_snapshot p "
            "WHERE p.batch_id IS NOT NULL ORDER BY p.snapshot_id"
        ).all()
        assert len(result) == 1
        assert result[0][0] == "new-btc"
        batch_id = result[0][1]
        balance = connection.exec_driver_sql(
            "SELECT snapshot_id, batch_id FROM balances_snapshot WHERE batch_id IS NOT NULL"
        ).one()
        assert balance[1] == batch_id
        assert (
            connection.exec_driver_sql("SELECT count(*) FROM portfolio_snapshots").scalar_one() == 1
        )
