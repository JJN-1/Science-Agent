"""sprint3 budget grants

FIX-02：新增预算豁免表。批准预算熔断时写入一条有额度、有有效期、可回溯到审批单的
豁免记录，使「批准 → 恢复运行」成为真实因果，而不是反复弹审批的死循环。

Revision ID: 9f3c1a7b5d20
Revises: 246a42e41036
Create Date: 2026-09-19 21:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '9f3c1a7b5d20'
down_revision: Union[str, None] = '246a42e41036'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('budget_grants',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('project_id', sa.Integer(), nullable=False),
    sa.Column('scope', sa.String(length=32), nullable=False),
    sa.Column('agent_id', sa.String(length=64), nullable=True),
    sa.Column('amount', sa.Float(), nullable=False),
    sa.Column('approval_id', sa.Integer(), nullable=True),
    sa.Column('expires_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['approval_id'], ['approvals.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_budget_grants_project_id'), 'budget_grants', ['project_id'], unique=False)
    op.create_index(op.f('ix_budget_grants_scope'), 'budget_grants', ['scope'], unique=False)
    op.create_index(op.f('ix_budget_grants_agent_id'), 'budget_grants', ['agent_id'], unique=False)
    op.create_index(op.f('ix_budget_grants_approval_id'), 'budget_grants', ['approval_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_budget_grants_approval_id'), table_name='budget_grants')
    op.drop_index(op.f('ix_budget_grants_agent_id'), table_name='budget_grants')
    op.drop_index(op.f('ix_budget_grants_scope'), table_name='budget_grants')
    op.drop_index(op.f('ix_budget_grants_project_id'), table_name='budget_grants')
    op.drop_table('budget_grants')
