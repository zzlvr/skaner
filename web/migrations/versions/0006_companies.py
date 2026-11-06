"""company cabinets: organizations, memberships, invitations; batches.org_id, created_by_user_id, wallet_user_id

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-21 22:00:00

As in 0004/0005, `users` is never rebuilt. `batches` is rebuilt by SQLite batch mode (it has no child tables, so
nothing cascades). Existing batches stay personal (org_id NULL); their author and payer are their user_id.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0006'
down_revision: Union[str, Sequence[str], None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('organizations',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('public_id', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=80), nullable=False),
    sa.Column('owner_user_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['owner_user_id'], ['users.id'], name=op.f('fk_organizations_owner_user_id_users'),
                            ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_organizations')),
    sa.UniqueConstraint('public_id', name=op.f('uq_organizations_public_id'))
    )
    with op.batch_alter_table('organizations', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_organizations_owner_user_id'), ['owner_user_id'], unique=True)

    op.create_table('memberships',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('org_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('role', sa.String(length=8), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_memberships_org_id_organizations'),
                            ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_memberships_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_memberships'))
    )
    with op.batch_alter_table('memberships', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_memberships_org_id'), ['org_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_memberships_user_id'), ['user_id'], unique=True)

    op.create_table('invitations',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('org_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=4), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=True),
    sa.Column('target_user_id', sa.Integer(), nullable=True),
    sa.Column('target_code', sa.String(length=12), nullable=True),
    sa.Column('created_by', sa.Integer(), nullable=True),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_invitations_created_by_users'),
                            ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_invitations_org_id_organizations'),
                            ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['target_user_id'], ['users.id'], name=op.f('fk_invitations_target_user_id_users'),
                            ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_invitations')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_invitations_token_hash'))
    )
    with op.batch_alter_table('invitations', schema=None) as batch_op:
        batch_op.create_index('ix_invitations_org_status', ['org_id', 'status'], unique=False)
        batch_op.create_index(batch_op.f('ix_invitations_expires_at'), ['expires_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_invitations_target_user_id'), ['target_user_id'], unique=False)

    with op.batch_alter_table('batches', schema=None) as batch_op:
        batch_op.add_column(sa.Column('org_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('created_by_user_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('wallet_user_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(batch_op.f('fk_batches_org_id_organizations'), 'organizations', ['org_id'],
                                    ['id'], ondelete='SET NULL')
        batch_op.create_foreign_key(batch_op.f('fk_batches_created_by_user_id_users'), 'users',
                                    ['created_by_user_id'], ['id'], ondelete='SET NULL')
        batch_op.create_foreign_key(batch_op.f('fk_batches_wallet_user_id_users'), 'users', ['wallet_user_id'],
                                    ['id'], ondelete='SET NULL')
        batch_op.create_index('ix_batches_org_created', ['org_id', 'created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_batches_wallet_user_id'), ['wallet_user_id'], unique=False)

    # Backfill: every existing batch is personal; its author and its payer are its user.
    op.execute("UPDATE batches SET created_by_user_id = user_id, wallet_user_id = user_id")


def downgrade() -> None:
    with op.batch_alter_table('batches', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_batches_wallet_user_id'))
        batch_op.drop_index('ix_batches_org_created')
        batch_op.drop_constraint(batch_op.f('fk_batches_wallet_user_id_users'), type_='foreignkey')
        batch_op.drop_constraint(batch_op.f('fk_batches_created_by_user_id_users'), type_='foreignkey')
        batch_op.drop_constraint(batch_op.f('fk_batches_org_id_organizations'), type_='foreignkey')
        batch_op.drop_column('wallet_user_id')
        batch_op.drop_column('created_by_user_id')
        batch_op.drop_column('org_id')
    with op.batch_alter_table('invitations', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_invitations_target_user_id'))
        batch_op.drop_index(batch_op.f('ix_invitations_expires_at'))
        batch_op.drop_index('ix_invitations_org_status')
    op.drop_table('invitations')
    with op.batch_alter_table('memberships', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_memberships_user_id'))
        batch_op.drop_index(batch_op.f('ix_memberships_org_id'))
    op.drop_table('memberships')
    with op.batch_alter_table('organizations', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_organizations_owner_user_id'))
    op.drop_table('organizations')
