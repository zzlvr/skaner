"""plans, account codes, payment requests, admin audit, tombstones; ledger rows survive account deletion

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-21 10:00:00

SQLite note: `users` is never rebuilt here (no batch mode on it). With foreign keys on, rebuilding a parent table
drops it, and dropping a parent fires ON DELETE CASCADE in sessions/batches/ledger. So users only gets plain
ADD COLUMN / CREATE INDEX (and DROP COLUMN on downgrade), which SQLite and PostgreSQL both do in place.
"""
import secrets
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0004'
down_revision: Union[str, Sequence[str], None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Same as web/accounts.py (copied: a migration must not change when the app code changes).
CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"


def _code() -> str:
    return "SK-" + "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))


def upgrade() -> None:
    op.add_column('users', sa.Column('account_code', sa.String(length=12), nullable=True))
    op.add_column('users', sa.Column('plan', sa.String(length=8), nullable=True))
    op.add_column('users', sa.Column('plan_quota', sa.Integer(), server_default='0', nullable=False))
    op.add_column('users', sa.Column('plan_pages_left', sa.Integer(), server_default='0', nullable=False))
    op.add_column('users', sa.Column('plan_expires_at', sa.DateTime(), nullable=True))
    op.add_column('users', sa.Column('blocked_at', sa.DateTime(), nullable=True))

    # Backfill: every existing user gets a unique code.
    conn = op.get_bind()
    users = sa.table('users', sa.column('id', sa.Integer), sa.column('account_code', sa.String))
    used = set()
    for (uid,) in conn.execute(sa.select(users.c.id).where(users.c.account_code.is_(None))).all():
        code = _code()
        while code in used:
            code = _code()
        used.add(code)
        conn.execute(users.update().where(users.c.id == uid).values(account_code=code))
    op.create_index(op.f('ix_users_account_code'), 'users', ['account_code'], unique=True)

    # Ledger: user_id may become NULL (account deleted, money record kept); new plan bucket column and a note.
    with op.batch_alter_table('ledger', schema=None) as batch_op:
        batch_op.add_column(sa.Column('plan_delta', sa.Integer(), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('note', sa.String(length=200), nullable=True))
        batch_op.drop_constraint('fk_ledger_user_id_users', type_='foreignkey')
        batch_op.alter_column('user_id', existing_type=sa.Integer(), nullable=True)
        batch_op.create_foreign_key(batch_op.f('fk_ledger_user_id_users'), 'users', ['user_id'], ['id'],
                                    ondelete='SET NULL')

    op.create_table('payment_requests',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('plan', sa.String(length=8), nullable=False),
    sa.Column('status', sa.String(length=8), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('done_at', sa.DateTime(), nullable=True),
    sa.Column('notified_at', sa.DateTime(), nullable=True),
    sa.Column('data_enc', sa.LargeBinary(), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_payment_requests_user_id_users'),
                            ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_payment_requests'))
    )
    with op.batch_alter_table('payment_requests', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_payment_requests_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_payment_requests_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_payment_requests_user_id'), ['user_id'], unique=False)

    op.create_table('admin_audit',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('admin_fp', sa.String(length=16), nullable=False),
    sa.Column('action', sa.String(length=24), nullable=False),
    sa.Column('target_user_id', sa.Integer(), nullable=True),
    sa.Column('target_code', sa.String(length=12), nullable=True),
    sa.Column('detail', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['target_user_id'], ['users.id'], name=op.f('fk_admin_audit_target_user_id_users'),
                            ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_admin_audit'))
    )
    with op.batch_alter_table('admin_audit', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_admin_audit_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_admin_audit_target_user_id'), ['target_user_id'], unique=False)

    op.create_table('tombstones',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('email_fp', sa.String(length=64), nullable=False),
    sa.Column('trial_used', sa.Boolean(), nullable=False),
    sa.Column('deleted_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_tombstones')),
    sa.UniqueConstraint('email_fp', name=op.f('uq_tombstones_email_fp'))
    )


def downgrade() -> None:
    op.drop_table('tombstones')
    with op.batch_alter_table('admin_audit', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_admin_audit_target_user_id'))
        batch_op.drop_index(batch_op.f('ix_admin_audit_created_at'))
    op.drop_table('admin_audit')
    with op.batch_alter_table('payment_requests', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_payment_requests_user_id'))
        batch_op.drop_index(batch_op.f('ix_payment_requests_status'))
        batch_op.drop_index(batch_op.f('ix_payment_requests_created_at'))
    op.drop_table('payment_requests')

    # The old schema cannot hold ledger rows without a user (deleted accounts): they are dropped on downgrade.
    op.execute("DELETE FROM ledger WHERE user_id IS NULL")
    with op.batch_alter_table('ledger', schema=None) as batch_op:
        batch_op.drop_constraint('fk_ledger_user_id_users', type_='foreignkey')
        batch_op.alter_column('user_id', existing_type=sa.Integer(), nullable=False)
        batch_op.create_foreign_key(batch_op.f('fk_ledger_user_id_users'), 'users', ['user_id'], ['id'],
                                    ondelete='CASCADE')
        batch_op.drop_column('note')
        batch_op.drop_column('plan_delta')

    op.drop_index(op.f('ix_users_account_code'), table_name='users')
    for col in ('blocked_at', 'plan_expires_at', 'plan_pages_left', 'plan_quota', 'plan', 'account_code'):
        op.drop_column('users', col)  # in place (ALTER TABLE ... DROP COLUMN): never rebuild `users`
