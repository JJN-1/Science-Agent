"""sprint3 llm_usage 失败记账

用户投诉「模型供应商统计里根本没有那条请求」。原因是记账只在**成功**时写：
调用失败（403 / 形状违规 / 超时）连一行都没有，于是统计既看不到请求次数，
也回答不了「到底发出去没有、发给了谁」。

新增三列：
- ``status``：ok | failed —— 让「失败」在统计里是一等公民，而不是空白
- ``error``：失败原因（含错误码与违规点），失败现场不用再去翻日志
- ``attempts``：实际发出的 HTTP 尝试次数。默认超时 120s × 4 次尝试 ≈ 8 分钟，
  「等了很久」是不是重试拖出来的，只有这个数字说得清

三列都给了 server_default，历史行无需回填即可升级。

Revision ID: b1c4e7a92d38
Revises: 7c1f9a34e5d2
Create Date: 2026-09-20 20:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b1c4e7a92d38'
down_revision: Union[str, None] = '7c1f9a34e5d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'llm_usage',
        sa.Column('status', sa.String(length=16), nullable=False, server_default='ok'),
    )
    op.add_column(
        'llm_usage',
        sa.Column('error', sa.Text(), nullable=True),
    )
    op.add_column(
        'llm_usage',
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='1'),
    )


def downgrade() -> None:
    op.drop_column('llm_usage', 'attempts')
    op.drop_column('llm_usage', 'error')
    op.drop_column('llm_usage', 'status')
