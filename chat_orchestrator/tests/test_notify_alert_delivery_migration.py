from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_notify_alert_delivery_migration_has_required_durable_contract() -> None:
    migration = (ROOT / "db/migrations/0028_notify_alert_deliveries.sql").read_text()
    schema = (ROOT / "db/schema/chat_db.sql").read_text()

    for sql in (migration, schema):
        assert "CREATE TABLE IF NOT EXISTS notify_alert_deliveries" in sql
        assert "UNIQUE (external_chat_id, external_message_id)" in sql
        assert "notify_alert_deliveries_grid_sent_idx" in sql
        assert "judgment" in sql
        assert "context_availability" in sql
        assert "send_decision" in sql
        assert "send_forced_by" in sql

    assert "ticket_id               uuid REFERENCES tickets(id) ON DELETE SET NULL" in migration
    assert "ticket_id       uuid," in schema


def test_downtime_marker_migration_matches_the_schema_snapshot() -> None:
    """The downtime clock is only trustworthy if the column and its partial
    index exist in both the migration and the checked-in schema."""
    migration = (ROOT / "db/migrations/0030_notify_alert_downtime.sql").read_text()
    schema = (ROOT / "db/schema/chat_db.sql").read_text()

    assert "ADD COLUMN IF NOT EXISTS downtime boolean NOT NULL DEFAULT false" in migration
    assert "downtime                boolean NOT NULL DEFAULT false" in schema
    for sql in (migration, schema):
        assert "notify_alert_deliveries_grid_downtime_sent_idx" in sql
        assert "WHERE downtime" in sql
