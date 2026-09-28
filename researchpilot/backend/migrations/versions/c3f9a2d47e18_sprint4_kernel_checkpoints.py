"""sprint4 kernel_checkpoints 表与 jobs.cancel_requested 列

内核检查点（US-407）。设计 §778 一句话定死了它的形状与用途：
**「内核检查点（计划、步骤序号、快照），支持中断后恢复」**。

三件东西各司其职，缺一不可：

1. **``plan_id`` + ``step_index``** —— 回答「跑到哪了」。``step_index`` 是**给人看的序号**
   （设计原文的「步骤序号」），而**恢复要靠快照里的 ``next_step_id``**：
   ``PlanStep.id`` 是稳定标识而不是下标（US-403 的铁律），用下标恢复会「插一步全部错位」。
   两者都留：序号用于「序号连续」这类断言与人读，标识用于真正定位。
2. **``snapshot``** —— 快照。只存「跑到第几步」是不够的：``react`` 模式**根本没有计划**，
   它的续跑点只能从快照里读（已跑多少轮、哪些工具调过）。快照还必须能独立回答
   「这次为什么停」—— 否则恢复时只能猜。
3. **``status``** —— ``running`` / ``paused`` / ``cancelled`` / ``done``。它是
   「能不能续跑」的判据：``done`` 之后再点「继续」是重跑，``cancelled`` 之后是续跑。
   把它压进 ``status`` 而不是靠「有没有下一条」推断，是因为**暂停与取消都不写终态行**。

⚠️ 为什么另起一张表而不是复用 ``stage_checkpoints``：那张表按
``(project_id, stage_id)`` 定位、服务的是**阶段级重跑**（设计 §6.4「任一阶段可单独重跑」）。
会话内核的坐标是 ``conversation_id``，且同一个阶段里可能先后跑过多次会话——
塞进同一张表会出现「两条 checkpoint 都叫 S5、不知道该从哪条续」。
**两个不同的恢复粒度，表也应该是两张。**

``jobs.cancel_requested`` 是协作式取消的信箱：正在执行的工作线程无法被安全打断
（同步 SQLAlchemy + 外部模型调用），所以取消只能做成「置一个标记，让循环在**下一个
安全点**自己停下」。``server_default='0'`` 让既有行在迁移时直接取到 ``false``——
不加默认值的 NOT NULL 加列会让迁移在真实库上当场失败。

Revision ID: c3f9a2d47e18
Revises: a7d3f8c21b64
Create Date: 2026-09-22 23:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c3f9a2d47e18'
down_revision: Union[str, None] = 'a7d3f8c21b64'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── jobs.cancel_requested ───────────────────
    # 协作式取消的信箱。``server_default='0'``：既有作业行在迁移时必须能直接取到 false，
    # 没有默认值的 NOT NULL 加列在真实库上会当场失败（老行没有值可填）。
    op.add_column(
        'jobs',
        sa.Column(
            'cancel_requested', sa.Boolean(), nullable=False, server_default=sa.text('0'),
        ),
    )

    # ── kernel_checkpoints ──────────────────────
    op.create_table(
        'kernel_checkpoints',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('conversation_id', sa.Integer(), nullable=False),
        # 可空 + SET NULL：`react` 模式没有计划；而计划行消失（会话被删）不该
        # 连带抹掉「当时跑到哪」—— 审计记录的删除条件只能是它自己的所有者没了。
        sa.Column('plan_id', sa.Integer(), nullable=True),
        #: 计划内的步骤序号（1 起）。给人读、给「序号连续」这类断言用；
        #: **恢复定位不用它**，用快照里的 next_step_id（见模块 docstring）
        sa.Column('step_index', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('snapshot', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ['conversation_id'], ['conversations.id'], ondelete='CASCADE'
        ),
        sa.ForeignKeyConstraint(['plan_id'], ['task_plans.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_kernel_checkpoints_conversation_id', 'kernel_checkpoints', ['conversation_id']
    )


def downgrade() -> None:
    op.drop_index(
        'ix_kernel_checkpoints_conversation_id', table_name='kernel_checkpoints'
    )
    op.drop_table('kernel_checkpoints')
    op.drop_column('jobs', 'cancel_requested')
