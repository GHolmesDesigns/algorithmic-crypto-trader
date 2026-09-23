import pytest
from core.models import TradingMode
from db.session import validate_database_url


def test_sqlite_is_allowed_only_for_backtest_or_test() -> None:
    validate_database_url("sqlite+pysqlite:///:memory:", TradingMode.BACKTEST)
    validate_database_url("sqlite+pysqlite:///:memory:", TradingMode.LIVE, "test")
    with pytest.raises(ValueError, match="SQLite"):
        validate_database_url("sqlite+pysqlite:///:memory:", TradingMode.PAPER)
