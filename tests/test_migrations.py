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


HISTORY_TABLES = ("signals", "risk_decisions", "orders", "fills", "system_events", "discrepancies")


def _alembic(url: str, monkeypatch):
    from alembic.config import Config

    # No ini file: alembic.ini's logging config would disable the test run's loggers.
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "alembic"))
    monkeypatch.setenv("DATABASE_URL", url)
    return config


def _indexes(engine) -> dict[str, set[tuple[str, tuple[str, ...]]]]:
    from sqlalchemy import inspect

    inspector = inspect(engine)
    return {
        table: {
            (index["name"], tuple(index["column_names"]))
            for index in inspector.get_indexes(table)
            if not index.get("unique")
        }
        for table in HISTORY_TABLES
    }


def test_history_index_migration_upgrades_downgrades_and_matches_the_models(
    tmp_path, monkeypatch
) -> None:
    from alembic import command
    from db.models import Base
    from sqlalchemy import text

    url = f"sqlite+pysqlite:///{tmp_path / 'migrated.db'}"
    config = _alembic(url, monkeypatch)
    engine = create_engine(url, future=True)
    command.upgrade(config, "0005_risk_decisions")
    before = _indexes(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO signals (signal_id, symbol, strategy_version, side, quantity, "
                "created_at) VALUES ('11111111111141118111111111111111', 'BTC-USD', 'v1', 'buy', "
                "0.01, '2026-09-27 12:00:00.000000')"
            )
        )

    command.upgrade(config, "head")
    upgraded = _indexes(engine)
    added = {table: upgraded[table] - before[table] for table in HISTORY_TABLES}
    migration = _load_migration("0006_history_indexes.py")
    assert {
        (name, table, columns) for table, rows in added.items() for name, columns in rows
    } == set(migration.INDEXES)

    # The models declare the same indexes, so create_all and the migrations agree.
    models = create_engine("sqlite+pysqlite://", future=True)
    Base.metadata.create_all(models)
    assert _indexes(models) == upgraded
    models.dispose()

    command.downgrade(config, "0005_risk_decisions")
    assert _indexes(engine) == before
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM signals")).scalar_one() == 1
        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert version == "0005_risk_decisions"

    command.upgrade(config, "head")
    assert _indexes(engine) == upgraded
    engine.dispose()


def _load_migration(name: str):
    path = Path(__file__).parents[1] / "alembic" / "versions" / name
    spec = importlib.util.spec_from_file_location(name.removesuffix(".py"), path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
