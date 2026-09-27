"""index the operator history reads: time windows, lineage joins, and ID lookups"""

from alembic import op

revision = "0006_history_indexes"
down_revision = "0005_risk_decisions"
branch_labels = None
depends_on = None

# Every history list filters one time column to a bounded window and pages newest
# first; the lineage view joins orders to their signal, risk decision, and fills.
INDEXES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("ix_signals_created_at", "signals", ("created_at",)),
    ("ix_risk_decisions_decided_at", "risk_decisions", ("decided_at",)),
    ("ix_risk_decisions_correlation_id", "risk_decisions", ("correlation_id",)),
    ("ix_orders_created_at", "orders", ("created_at",)),
    ("ix_orders_signal_id", "orders", ("signal_id",)),
    ("ix_orders_risk_approval_id", "orders", ("risk_approval_id",)),
    ("ix_orders_correlation_id", "orders", ("correlation_id",)),
    ("ix_fills_order_id", "fills", ("order_id",)),
    ("ix_system_events_created_at", "system_events", ("created_at",)),
    ("ix_system_events_event_type_created_at", "system_events", ("event_type", "created_at")),
    ("ix_discrepancies_created_at", "discrepancies", ("created_at",)),
)


def upgrade() -> None:
    for name, table, columns in INDEXES:
        op.create_index(name, table, list(columns))


def downgrade() -> None:
    for name, table, _columns in reversed(INDEXES):
        op.drop_index(name, table_name=table)
