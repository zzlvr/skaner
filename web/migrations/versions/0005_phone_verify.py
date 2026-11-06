"""phone confirmation through the Telegram bot: users.phone_fp, phone_challenges, phone_trials

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-21 18:00:00

As in 0004, `users` is never rebuilt (SQLite would cascade-delete child rows): only ADD COLUMN and CREATE INDEX.
A unique index allows many NULLs on SQLite and PostgreSQL, so existing users (no phone) all fit.
Only fingerprints and token hashes are stored, never a phone number or a token.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0005'
down_revision: Union[str, Sequence[str], None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('phone_fp', sa.String(length=64), nullable=True))
    op.create_index(op.f('ix_users_phone_fp'), 'users', ['phone_fp'], unique=True)

    op.create_table('phone_challenges',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('ip_fp', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.Column('used_at', sa.DateTime(), nullable=True),
    sa.Column('outcome', sa.String(length=16), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_phone_challenges_user_id_users'),
                            ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_phone_challenges')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_phone_challenges_token_hash'))
    )
    with op.batch_alter_table('phone_challenges', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_phone_challenges_user_id'), ['user_id'], unique=True)

    op.create_table('phone_trials',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('phone_fp', sa.String(length=64), nullable=False),
    sa.Column('granted_at', sa.DateTime(), nullable=False),
    sa.Column('first_user_id', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['first_user_id'], ['users.id'], name=op.f('fk_phone_trials_first_user_id_users'),
                            ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_phone_trials')),
    sa.UniqueConstraint('phone_fp', name=op.f('uq_phone_trials_phone_fp'))
    )
    with op.batch_alter_table('phone_trials', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_phone_trials_first_user_id'), ['first_user_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('phone_trials', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_phone_trials_first_user_id'))
    op.drop_table('phone_trials')
    with op.batch_alter_table('phone_challenges', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_phone_challenges_user_id'))
    op.drop_table('phone_challenges')
    op.drop_index(op.f('ix_users_phone_fp'), table_name='users')
    op.drop_column('users', 'phone_fp')  # in place: never rebuild `users`
