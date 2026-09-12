"""add sources

Revision ID: b7f3a1c9d2e4
Revises: 9de205bfdf73
Create Date: 2026-09-12 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b7f3a1c9d2e4'
down_revision: Union[str, None] = '9de205bfdf73'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'sources',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('hostname', sa.String(length=255), nullable=False),
        sa.Column('share', sa.String(length=255), nullable=False),
        sa.Column('mount_path', sa.String(length=512), nullable=False),
        sa.Column('credentials_path', sa.String(length=512), nullable=False),
        sa.Column('unit_name', sa.String(length=255), nullable=False),
        sa.Column('smb_version', sa.String(length=16), nullable=False),
        sa.Column('domain', sa.String(length=128), nullable=True),
        sa.Column('username', sa.String(length=255), nullable=False),
        sa.Column(
            'last_health',
            sa.Enum('unknown', 'healthy', 'unhealthy', name='sourcehealth',
                    native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column('last_checked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('hostname', name='uq_source_hostname'),
    )
    with op.batch_alter_table('sources', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_sources_hostname'), ['hostname'], unique=False)
        batch_op.create_index(batch_op.f('ix_sources_last_health'), ['last_health'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('sources', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sources_last_health'))
        batch_op.drop_index(batch_op.f('ix_sources_hostname'))

    op.drop_table('sources')
