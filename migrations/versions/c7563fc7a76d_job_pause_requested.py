"""job pause_requested

Adds ``Job.pause_requested`` (mirrors ``cancel_requested``) for the
pause/resume feature on running write jobs. The new ``JobStatus.paused``
value is stored in the existing ``status`` varchar column (native_enum=False)
and needs no schema change of its own.

Revision ID: c7563fc7a76d
Revises: bfc167b72cb8
Create Date: 2026-09-16 08:15:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c7563fc7a76d'
down_revision: Union[str, None] = 'bfc167b72cb8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('pause_requested', sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.drop_column('pause_requested')
