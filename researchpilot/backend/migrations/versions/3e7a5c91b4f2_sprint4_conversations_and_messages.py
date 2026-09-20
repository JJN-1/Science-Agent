"""sprint4 conversations 与 messages

Agent 内核（Sprint 4）的第一步：把「对话」变成一等公民，而不是把研究目标
塞进一个自由文本字段。两张表的分工刻意不对等：

- ``conversations`` 只有一个 ``project_id`` 指向项目，**不持有任何研究状态**。
  研究状态始终在结构化黑板（设计 §352 / D2）—— 消息记录丢了，研究进度不受影响。
- ``messages`` 是入口与呈现层：用户说了什么、助手回了什么、工具返回了什么。
  ``tool_call_id`` 让「发起调用」与「回填结果」能配对，这样工具执行的结果
  才有归属，出问题时能追到具体是哪一次调用。

``tokens`` 存**本地估算值**（自写启发式），供下一次裁剪算预算；上游真实用量
在 ``llm_usage`` 里。刻意不复用同一列 —— 混成一个数字后，事后分不清哪个能信。

Revision ID: 3e7a5c91b4f2
Revises: b1c4e7a92d38
Create Date: 2026-09-20 22:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '3e7a5c91b4f2'
down_revision: Union[str, None] = 'b1c4e7a92d38'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'conversations',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_conversations_project_id', 'conversations', ['project_id'])
    op.create_index('ix_conversations_status', 'conversations', ['status'])

    op.create_table(
        'messages',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('conversation_id', sa.Integer(), nullable=False),
        sa.Column('role', sa.String(length=16), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('tool_call_id', sa.String(length=64), nullable=True),
        sa.Column('tokens', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ['conversation_id'], ['conversations.id'], ondelete='CASCADE'
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_messages_conversation_id', 'messages', ['conversation_id'])
    op.create_index('ix_messages_tool_call_id', 'messages', ['tool_call_id'])


def downgrade() -> None:
    op.drop_index('ix_messages_tool_call_id', table_name='messages')
    op.drop_index('ix_messages_conversation_id', table_name='messages')
    op.drop_table('messages')
    op.drop_index('ix_conversations_status', table_name='conversations')
    op.drop_index('ix_conversations_project_id', table_name='conversations')
    op.drop_table('conversations')
