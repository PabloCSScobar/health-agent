"""dashboard, korelacje, zdjęcia, suplementy i przypomnienia

Revision ID: 2a9d6c781b42
Revises: 9b4f2e6d31a8
Create Date: 2026-09-19
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "2a9d6c781b42"
down_revision: Union[str, Sequence[str], None] = "9b4f2e6d31a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "dashboard_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("csrf_token", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_dashboard_sessions_token_hash", "dashboard_sessions", ["token_hash"], unique=True)
    op.create_index("ix_dashboard_sessions_expires_at", "dashboard_sessions", ["expires_at"])

    op.create_table(
        "correlation_results",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("method_version", sa.String(length=32), nullable=False),
        sa.Column("pair_key", sa.String(length=64), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("rho", sa.Float(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("details_json", sa.JSON(), nullable=True),
        sa.Column("knowledge_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["knowledge_id"], ["knowledge.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "method_version", "pair_key", "period_end",
            name="uq_correlation_method_pair_period",
        ),
    )
    op.create_index("ix_correlation_results_pair_key", "correlation_results", ["pair_key"])
    op.create_index("ix_correlation_results_period_end", "correlation_results", ["period_end"])
    op.create_index("ix_correlation_results_knowledge_id", "correlation_results", ["knowledge_id"])

    op.create_table(
        "progress_photos",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("captured_date", sa.Date(), nullable=False),
        sa.Column("view", sa.String(length=16), nullable=False),
        sa.Column("note", sa.String(), nullable=True),
        sa.Column("storage_key", sa.String(length=256), nullable=False),
        sa.Column("content_type", sa.String(length=64), nullable=False),
        sa.Column("bytes_size", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("view IN ('front', 'side', 'back', 'other')", name="ck_progress_photos_view"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sha256", name="uq_progress_photos_sha256"),
        sa.UniqueConstraint("storage_key"),
    )
    op.create_index("ix_progress_photos_captured_date", "progress_photos", ["captured_date"])
    op.create_index("ix_progress_photos_view", "progress_photos", ["view"])
    op.create_index("ix_progress_photos_state", "progress_photos", ["state"])

    op.create_table(
        "supplements",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("dose", sa.String(length=128), nullable=True),
        sa.Column("notes", sa.String(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_index("ix_supplements_active", "supplements", ["active"])

    op.create_table(
        "reminder_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("local_time", sa.String(length=5), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("schedule_json", sa.JSON(), nullable=False),
        sa.Column("condition_type", sa.String(length=32), nullable=True),
        sa.Column("condition_threshold", sa.Float(), nullable=True),
        sa.Column("condition_window_hours", sa.Integer(), nullable=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('draft', 'active', 'paused')", name="ck_reminder_rules_status"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_reminder_rules_kind", "reminder_rules", ["kind"])
    op.create_index("ix_reminder_rules_status", "reminder_rules", ["status"])

    op.create_table(
        "data_freshness",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("metric", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("observed_through", sa.Date(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("metric", "source", name="uq_data_freshness_metric_source"),
    )
    op.create_index("ix_data_freshness_metric", "data_freshness", ["metric"])

    op.create_table(
        "supplement_intakes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("supplement_id", sa.Integer(), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("note", sa.String(), nullable=True),
        sa.CheckConstraint("status IN ('taken', 'skipped')", name="ck_supplement_intake_status"),
        sa.ForeignKeyConstraint(["supplement_id"], ["supplements.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("supplement_id", "scheduled_for", name="uq_supplement_intake_schedule"),
    )
    op.create_index("ix_supplement_intakes_supplement_id", "supplement_intakes", ["supplement_id"])

    op.create_table(
        "reminder_occurrences",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("rule_id", sa.Integer(), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["rule_id"], ["reminder_rules.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("rule_id", "scheduled_for", name="uq_reminder_occurrence_schedule"),
    )
    op.create_index("ix_reminder_occurrences_rule_id", "reminder_occurrences", ["rule_id"])
    op.create_index("ix_reminder_occurrences_scheduled_for", "reminder_occurrences", ["scheduled_for"])
    op.create_index("ix_reminder_occurrences_status", "reminder_occurrences", ["status"])
    op.create_index("ix_reminder_occurrences_next_attempt_at", "reminder_occurrences", ["next_attempt_at"])

    op.create_table(
        "notification_outbox",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("occurrence_id", sa.Integer(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["occurrence_id"], ["reminder_occurrences.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("occurrence_id", name="uq_notification_outbox_occurrence"),
    )
    op.create_index("ix_notification_outbox_occurrence_id", "notification_outbox", ["occurrence_id"])
    op.create_index("ix_notification_outbox_status", "notification_outbox", ["status"])
    op.create_index("ix_notification_outbox_available_at", "notification_outbox", ["available_at"])


def downgrade() -> None:
    op.drop_table("notification_outbox")
    op.drop_table("reminder_occurrences")
    op.drop_table("supplement_intakes")
    op.drop_table("data_freshness")
    op.drop_table("reminder_rules")
    op.drop_table("supplements")
    op.drop_table("progress_photos")
    op.drop_table("correlation_results")
    op.drop_table("dashboard_sessions")
