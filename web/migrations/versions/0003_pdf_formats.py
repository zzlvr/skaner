"""PDF formats: longer format names, short-lived scan files, free scan counters

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-20 23:30:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0003'
down_revision: Union[str, Sequence[str], None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('batches', schema=None) as batch_op:
        batch_op.alter_column('fmt', existing_type=sa.String(length=8), type_=sa.String(length=16),
                              existing_nullable=False)
        batch_op.add_column(sa.Column('file_enc', sa.LargeBinary(), nullable=True))
        batch_op.add_column(sa.Column('file_expires_at', sa.DateTime(), nullable=True))
        batch_op.create_index(batch_op.f('ix_batches_file_expires_at'), ['file_expires_at'], unique=False)

    op.create_table('scan_usage',
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('day', sa.String(length=10), nullable=False),
    sa.Column('pages', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_scan_usage_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id', 'day', name=op.f('pk_scan_usage'))
    )
    op.create_table('scan_daily',
    sa.Column('day', sa.String(length=10), nullable=False),
    sa.Column('pages', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('day', name=op.f('pk_scan_daily'))
    )


def downgrade() -> None:
    op.drop_table('scan_daily')
    op.drop_table('scan_usage')
    op.execute("DELETE FROM batches WHERE fmt IN ('pdf_scan', 'pdf_text')")  # the old schema has no such formats
    with op.batch_alter_table('batches', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_batches_file_expires_at'))
        batch_op.drop_column('file_expires_at')
        batch_op.drop_column('file_enc')
        batch_op.alter_column('fmt', existing_type=sa.String(length=16), type_=sa.String(length=8),
                              existing_nullable=False)
