"""separate daily cap for trial-funded work: daily_usage.trial_pages, batches.trial_funded

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-22 12:00:00

Both columns are added in place (ALTER TABLE ... ADD COLUMN, no table rebuild) with a server default, so existing
rows keep working: old days count 0 trial pages, old batches are not trial-funded (their correction, if any is
still pending, touches only the total, exactly as before).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0007'
down_revision: Union[str, Sequence[str], None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('daily_usage', sa.Column('trial_pages', sa.Integer(), server_default='0', nullable=False))
    op.add_column('batches', sa.Column('trial_funded', sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade() -> None:
    op.drop_column('batches', 'trial_funded')  # in place (ALTER TABLE ... DROP COLUMN)
    op.drop_column('daily_usage', 'trial_pages')
