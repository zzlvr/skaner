"""guest accounts: users.is_guest

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-09 12:00:00

Added in place with a server default: every existing account is a normal (signed-in) account.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0009'
down_revision: Union[str, Sequence[str], None] = '0008'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('is_guest', sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade() -> None:
    op.drop_column('users', 'is_guest')
