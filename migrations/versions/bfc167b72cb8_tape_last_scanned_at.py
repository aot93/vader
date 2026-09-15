"""tape last_scanned_at

Distinguishes "listed but never hash-verified" from "fully verified" at the
tape level (plan item 5, fast-scan mode). A fast tape_import sets this
instead of last_verified_at; a full import/verify still sets both.

Revision ID: bfc167b72cb8
Revises: f2e3d4c5b6a7
Create Date: 2026-09-15 14:07:47.025598
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'bfc167b72cb8'
down_revision: Union[str, None] = 'f2e3d4c5b6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('tapes', schema=None) as batch_op:
        batch_op.add_column(sa.Column('last_scanned_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('tapes', schema=None) as batch_op:
        batch_op.drop_column('last_scanned_at')
