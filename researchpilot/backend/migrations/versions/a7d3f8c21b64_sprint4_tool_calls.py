"""sprint4 tool_calls 表与 messages.tool_calls 列

内核循环（US-405）要落两类东西，它们服务于两个不同的用途，因此分成两处：

1. **``messages.tool_calls``**（assistant 消息上的 JSON 列）—— 协议要求。
   OpenAI 协议的硬约束是：``role="tool"`` 的消息**必须**能对应上一条带 ``tool_calls``
   的 ``assistant`` 消息，缺任一条整条请求会被 400 拒收。而会话历史是**跨请求**复用的：
   第 5 步把工具结果写进 ``messages`` 之后，用户再说第二句话时，装配出来的历史里就
   必然同时含 tool 结果与它的 assistant 调用。不存这一列，第二次对话就会被上游拒收 ——
   而且是在「第一轮明明跑通了」之后才出现，最难归因的一类故障。
   只存 ``tool_call_id``（结果侧）而不存调用侧，等于只存了半条关系。

2. **``tool_calls`` 表** —— 审计要求（D12）。「模型说要调什么」与「系统真的调了什么」
   必须能分开回答。事件流（``tool.call``/``tool.result``）是**实时**的那一份，
   这张表是**可查询**的那一份：会话里调过多少个工具、哪些被拒绝、各自耗时多少。

``approval_id`` 本步恒为 NULL：危险操作的批准接线在第 6 步（与 ``messages.job_id``
在第 1 步恒为 NULL 同一条约定 —— 字段先就位，值随后填）。

Revision ID: a7d3f8c21b64
Revises: 8b2f6c04d1e9
Create Date: 2026-09-21 23:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a7d3f8c21b64'
down_revision: Union[str, None] = '8b2f6c04d1e9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── messages.tool_calls ─────────────────────
    # 可空：绝大多数消息没有工具调用，且它是**新增**列 —— 既有行天然是 NULL，
    # 不能设 NOT NULL，否则迁移会在真实库上失败（不是「不该失败」，是根本没有默认值可填）
    op.add_column('messages', sa.Column('tool_calls', sa.JSON(), nullable=True))

    # ── tool_calls ──────────────────────────────
    op.create_table(
        'tool_calls',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('conversation_id', sa.Integer(), nullable=False),
        sa.Column('run_id', sa.Integer(), nullable=True),
        sa.Column('tool_name', sa.String(length=64), nullable=False),
        sa.Column('args', sa.JSON(), nullable=False),
        # 权限等级**取自工具的静态契约**（D5），不是模型自称 —— 用模型给的值，
        # 一个被提示注入污染的模型会立刻把 dangerous 说成 read
        sa.Column('permission', sa.String(length=16), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('result', sa.JSON(), nullable=True),
        sa.Column('error', sa.Text(), nullable=False),
        sa.Column('approval_id', sa.Integer(), nullable=True),
        sa.Column('duration_ms', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ['conversation_id'], ['conversations.id'], ondelete='CASCADE'
        ),
        # SET NULL 而非 CASCADE：run 被清理不该连带抹掉「当时调过这个工具」的记录。
        # 审计记录的删除条件只能是「这个会话没了」，不能是「那次运行没了」。
        sa.ForeignKeyConstraint(['run_id'], ['agent_runs.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['approval_id'], ['approvals.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_tool_calls_conversation_id', 'tool_calls', ['conversation_id'])
    op.create_index('ix_tool_calls_run_id', 'tool_calls', ['run_id'])


def downgrade() -> None:
    op.drop_index('ix_tool_calls_run_id', table_name='tool_calls')
    op.drop_index('ix_tool_calls_conversation_id', table_name='tool_calls')
    op.drop_table('tool_calls')
    op.drop_column('messages', 'tool_calls')
