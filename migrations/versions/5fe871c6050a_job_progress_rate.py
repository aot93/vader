"""job progress rate

Adds ``Job.progress_rate_bytes_per_sec`` -- an exponentially-smoothed
bytes/sec figure updated on every progress() call, backing the speed (MB/s)
and ETA shown on a running write job's page. Nullable: only write jobs
report byte-based progress (everything else counts items/tapes), and a job
has no rate at all until its second progress update.

Revision ID: 5fe871c6050a
Revises: c7563fc7a76d
Create Date: 2026-09-21 13:05:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '5fe871c6050a'
down_revision: Union[str, None] = 'c7563fc7a76d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('progress_rate_bytes_per_sec', sa.Float(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.drop_column('progress_rate_bytes_per_sec')
