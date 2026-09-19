"""feedback powiazany z rozmowa i agent_run

Revision ID: 9b4f2e6d31a8
Revises: 4cb0a4737e01
Create Date: 2026-09-19
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "9b4f2e6d31a8"
down_revision: Union[str, Sequence[str], None] = "4cb0a4737e01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("telegram_message_id", sa.BigInteger(), nullable=True))
    op.add_column("conversations", sa.Column("telegram_message_ids", sa.JSON(), nullable=True))
    op.add_column("conversations", sa.Column("root_run_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_conversations_root_run_id_agent_runs",
        "conversations", "agent_runs", ["root_run_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index("ix_conversations_telegram_message_id", "conversations", ["telegram_message_id"])
    op.create_index("ix_conversations_root_run_id", "conversations", ["root_run_id"])
    op.create_unique_constraint(
        "uq_conversations_chat_message", "conversations", ["chat_id", "telegram_message_id"]
    )
    op.create_table(
        "feedback",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("agent_run_id", sa.Integer(), nullable=True),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("comment", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("rating IN (-1, 1)", name="ck_feedback_rating"),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("conversation_id", name="uq_feedback_conversation"),
    )
    op.create_index("ix_feedback_conversation_id", "feedback", ["conversation_id"])
    op.create_index("ix_feedback_agent_run_id", "feedback", ["agent_run_id"])


def downgrade() -> None:
    op.drop_index("ix_feedback_agent_run_id", table_name="feedback")
    op.drop_index("ix_feedback_conversation_id", table_name="feedback")
    op.drop_table("feedback")
    op.drop_constraint("uq_conversations_chat_message", "conversations", type_="unique")
    op.drop_index("ix_conversations_root_run_id", table_name="conversations")
    op.drop_index("ix_conversations_telegram_message_id", table_name="conversations")
    op.drop_constraint("fk_conversations_root_run_id_agent_runs", "conversations", type_="foreignkey")
    op.drop_column("conversations", "root_run_id")
    op.drop_column("conversations", "telegram_message_ids")
    op.drop_column("conversations", "telegram_message_id")
