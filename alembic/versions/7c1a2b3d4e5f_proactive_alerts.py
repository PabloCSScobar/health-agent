"""proaktywne alerty i zakresy synchronizacji

Revision ID: 7c1a2b3d4e5f
Revises: 2a9d6c781b42
Create Date: 2026-09-19
"""

import datetime as dt
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "7c1a2b3d4e5f"
down_revision: Union[str, Sequence[str], None] = "2a9d6c781b42"
branch_labels = None
depends_on = None


def upgrade() -> None:
    now = dt.datetime.now(dt.timezone.utc)
    op.add_column("reminder_rules", sa.Column("system_key", sa.String(64), nullable=True))
    op.create_unique_constraint("uq_reminder_rules_system_key", "reminder_rules", ["system_key"])
    op.create_table(
        "data_sync_ranges",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("metric", sa.String(32), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("range_start", sa.Date(), nullable=False),
        sa.Column("range_end", sa.Date(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("range_start <= range_end", name="ck_data_sync_range_order"),
        sa.UniqueConstraint("metric", "source", "range_start", "range_end", name="uq_data_sync_range"),
    )
    op.create_index("ix_data_sync_ranges_metric", "data_sync_ranges", ["metric"])
    op.create_table(
        "proactive_alert_settings",
        sa.Column("topic", sa.String(32), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    settings_table = sa.table(
        "proactive_alert_settings",
        sa.column("topic", sa.String()), sa.column("enabled", sa.Boolean()),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    op.bulk_insert(settings_table, [
        {"topic": "no_training", "enabled": False, "updated_at": now},
        {"topic": "low_protein", "enabled": False, "updated_at": now},
        {"topic": "weight_rising", "enabled": False, "updated_at": now},
    ], multiinsert=False)
    rules = sa.table(
        "reminder_rules",
        sa.column("system_key", sa.String()), sa.column("kind", sa.String()),
        sa.column("title", sa.String()), sa.column("local_time", sa.String()),
        sa.column("timezone", sa.String()), sa.column("schedule_json", sa.JSON()),
        sa.column("condition_type", sa.String()), sa.column("condition_threshold", sa.Float()),
        sa.column("condition_window_hours", sa.Integer()), sa.column("payload_json", sa.JSON()),
        sa.column("status", sa.String()), sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    op.bulk_insert(rules, [{
        "system_key": "proactive_alerts", "kind": "proactive_alert",
        "title": "Proaktywne alerty", "local_time": "20:00", "timezone": "Europe/Warsaw",
        "schedule_json": {"weekdays": list(range(7))}, "condition_type": None,
        "condition_threshold": None, "condition_window_hours": None, "payload_json": {},
        "status": "paused", "created_at": now, "updated_at": now,
    }], multiinsert=False)
    op.create_table(
        "proactive_alert_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("occurrence_id", sa.Integer(), nullable=False),
        sa.Column("topic", sa.String(32), nullable=False),
        sa.Column("details_json", sa.JSON(), nullable=False),
        sa.Column("qualified_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["occurrence_id"], ["reminder_occurrences.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("occurrence_id", "topic", name="uq_proactive_alert_event_topic"),
    )
    op.create_index("ix_proactive_alert_events_occurrence_id", "proactive_alert_events", ["occurrence_id"])
    op.create_index("ix_proactive_alert_events_topic", "proactive_alert_events", ["topic"])


def downgrade() -> None:
    op.drop_table("proactive_alert_events")
    op.execute("DELETE FROM reminder_rules WHERE system_key = 'proactive_alerts'")
    op.drop_table("proactive_alert_settings")
    op.drop_table("data_sync_ranges")
    op.drop_constraint("uq_reminder_rules_system_key", "reminder_rules", type_="unique")
    op.drop_column("reminder_rules", "system_key")
