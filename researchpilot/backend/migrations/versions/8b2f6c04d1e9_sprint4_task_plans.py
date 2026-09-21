"""sprint4 task_plans 结构化任务计划

计划必须**结构化落库**，理由有两条，缺一条都不值得为它单独建表：

1. **阶段二的科研模式就是替换这张表的计划模板**（两阶段衔接约定 2）。存文本的话，
   「替换模板」只能靠重写提示词，而模板与提示词混在一起之后就没法验证模板对不对。
2. **G2 第 7 条要断言「同一输入两次跑出相同步骤序列」**。文本计划只能比字符串，
   而那会把「换了个措辞」也判成不一致 —— 判据必须落在结构上。

两个字段刻意从内存搬进库：

- ``deterministic``：确定性是给复现与消融实验用的（设计 §12.4）。只把它放在运行时
  变量里，事后就答不出「这次跑到底是不是确定性的」。
- ``seed``：确定性要求固定种子。种子不落库，同一份计划重跑就没有可复现的依据。

Revision ID: 8b2f6c04d1e9
Revises: 3e7a5c91b4f2
Create Date: 2026-09-21 21:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '8b2f6c04d1e9'
down_revision: Union[str, None] = '3e7a5c91b4f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'task_plans',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('conversation_id', sa.Integer(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('mode', sa.String(length=16), nullable=False),
        sa.Column('deterministic', sa.Boolean(), nullable=False),
        sa.Column('seed', sa.Integer(), nullable=True),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('rationale', sa.Text(), nullable=False),
        sa.Column('steps', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ['conversation_id'], ['conversations.id'], ondelete='CASCADE'
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_task_plans_conversation_id', 'task_plans', ['conversation_id'])
    op.create_index('ix_task_plans_status', 'task_plans', ['status'])


def downgrade() -> None:
    op.drop_index('ix_task_plans_status', table_name='task_plans')
    op.drop_index('ix_task_plans_conversation_id', table_name='task_plans')
    op.drop_table('task_plans')
