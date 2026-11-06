"""bring-your-own-key batches: batches.byok

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-06 12:00:00

Added in place with a server default: existing batches were all paid from the page balance (byok = false).
The key itself is never stored; the column only says that the batch was not charged.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0008'
down_revision: Union[str, Sequence[str], None] = '0007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('batches', sa.Column('byok', sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade() -> None:
    op.drop_column('batches', 'byok')
