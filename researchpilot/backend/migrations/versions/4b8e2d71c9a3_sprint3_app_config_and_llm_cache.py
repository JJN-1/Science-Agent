"""sprint3 app_config & llm_cache

FIX-05：把熔断状态与模型响应缓存从进程内存搬到数据库。

- ``app_config``：通用键值表，承载运行期状态（本冲刺用于 ``circuit:<provider>``）。
  设计 §11.4 提到过这张表，但此前从未创建。
- ``llm_cache``：带 TTL 的响应缓存，键由 (provider, model, tier, messages, schema)
  规范化哈希得到，因此降级作答的结果会记在实际响应方名下，不会污染首候选的键。

Revision ID: 4b8e2d71c9a3
Revises: 9f3c1a7b5d20
Create Date: 2026-09-19 22:55:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '4b8e2d71c9a3'
down_revision: Union[str, None] = '9f3c1a7b5d20'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('app_config',
    sa.Column('key', sa.String(length=128), nullable=False),
    sa.Column('value', sa.JSON(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('key')
    )

    op.create_table('llm_cache',
    sa.Column('cache_key', sa.String(length=64), nullable=False),
    sa.Column('provider', sa.String(length=64), nullable=False),
    sa.Column('model', sa.String(length=128), nullable=False),
    sa.Column('tier', sa.String(length=32), nullable=False),
    sa.Column('response', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('cache_key')
    )
    op.create_index(op.f('ix_llm_cache_expires_at'), 'llm_cache', ['expires_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_llm_cache_expires_at'), table_name='llm_cache')
    op.drop_table('llm_cache')
    op.drop_table('app_config')
