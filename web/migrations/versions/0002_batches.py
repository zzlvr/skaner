"""batches, ledger, daily usage, trial balance

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-20 22:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0002'
down_revision: Union[str, Sequence[str], None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('trial_granted_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('trial_pages_left', sa.Integer(), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('trial_expires_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('paid_pages_left', sa.Integer(), server_default='0', nullable=False))

    op.create_table('batches',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('public_id', sa.String(length=32), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('doctype', sa.String(length=16), nullable=False),
    sa.Column('fmt', sa.String(length=8), nullable=False),
    sa.Column('one_document', sa.Boolean(), nullable=False),
    sa.Column('lang', sa.String(length=2), nullable=False),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('files_count', sa.Integer(), nullable=False),
    sa.Column('files_done', sa.Integer(), nullable=False),
    sa.Column('pages_estimated', sa.Integer(), nullable=False),
    sa.Column('pages_charged', sa.Integer(), nullable=False),
    sa.Column('work_units', sa.Integer(), nullable=False),
    sa.Column('docs_found', sa.Integer(), nullable=False),
    sa.Column('needs_review', sa.Integer(), nullable=False),
    sa.Column('failed_files', sa.Integer(), nullable=False),
    sa.Column('not_found', sa.Integer(), nullable=False),
    sa.Column('error_code', sa.String(length=32), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('started_at', sa.DateTime(), nullable=True),
    sa.Column('finished_at', sa.DateTime(), nullable=True),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.Column('results_enc', sa.LargeBinary(), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_batches_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_batches')),
    sa.UniqueConstraint('public_id', name=op.f('uq_batches_public_id'))
    )
    with op.batch_alter_table('batches', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_batches_expires_at'), ['expires_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_batches_status'), ['status'], unique=False)
        batch_op.create_index('ix_batches_user_created', ['user_id', 'created_at'], unique=False)

    op.create_table('ledger',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('batch_ref', sa.String(length=32), nullable=True),
    sa.Column('reason', sa.String(length=16), nullable=False),
    sa.Column('trial_delta', sa.Integer(), nullable=False),
    sa.Column('paid_delta', sa.Integer(), nullable=False),
    sa.Column('charged', sa.Integer(), nullable=False),
    sa.Column('overrun', sa.Integer(), nullable=False),
    sa.Column('work_units', sa.Integer(), nullable=False),
    sa.Column('free_failures', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_ledger_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_ledger'))
    )
    with op.batch_alter_table('ledger', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_ledger_batch_ref'), ['batch_ref'], unique=False)
        batch_op.create_index(batch_op.f('ix_ledger_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_ledger_user_id'), ['user_id'], unique=False)

    op.create_table('daily_usage',
    sa.Column('day', sa.String(length=10), nullable=False),
    sa.Column('pages', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('day', name=op.f('pk_daily_usage'))
    )


def downgrade() -> None:
    op.drop_table('daily_usage')
    with op.batch_alter_table('ledger', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_ledger_user_id'))
        batch_op.drop_index(batch_op.f('ix_ledger_created_at'))
        batch_op.drop_index(batch_op.f('ix_ledger_batch_ref'))
    op.drop_table('ledger')
    with op.batch_alter_table('batches', schema=None) as batch_op:
        batch_op.drop_index('ix_batches_user_created')
        batch_op.drop_index(batch_op.f('ix_batches_status'))
        batch_op.drop_index(batch_op.f('ix_batches_expires_at'))
    op.drop_table('batches')
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('paid_pages_left')
        batch_op.drop_column('trial_expires_at')
        batch_op.drop_column('trial_pages_left')
        batch_op.drop_column('trial_granted_at')
