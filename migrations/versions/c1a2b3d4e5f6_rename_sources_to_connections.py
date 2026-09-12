"""rename sources to connections; add purpose + restore destinations

Revision ID: c1a2b3d4e5f6
Revises: b7f3a1c9d2e4
Create Date: 2026-09-15 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c1a2b3d4e5f6'
down_revision: Union[str, None] = 'b7f3a1c9d2e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.rename_table('sources', 'connections')

    with op.batch_alter_table('connections', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'purpose',
                sa.Enum('ingest', 'restore_destination', name='connectionpurpose',
                        native_enum=False, length=32),
                nullable=False, server_default='ingest',
            )
        )
        batch_op.drop_constraint('uq_source_hostname', type_='unique')
        batch_op.create_unique_constraint(
            'uq_connection_hostname_purpose', ['hostname', 'purpose']
        )
        batch_op.drop_index(batch_op.f('ix_sources_hostname'))
        batch_op.drop_index(batch_op.f('ix_sources_last_health'))
        batch_op.create_index(batch_op.f('ix_connections_hostname'), ['hostname'], unique=False)
        batch_op.create_index(batch_op.f('ix_connections_last_health'), ['last_health'], unique=False)
        batch_op.create_index(batch_op.f('ix_connections_purpose'), ['purpose'], unique=False)

    with op.batch_alter_table('restore_requests', schema=None) as batch_op:
        batch_op.add_column(sa.Column('destination_connection_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_restore_requests_destination_connection_id_connections',
            'connections', ['destination_connection_id'], ['id'],
        )


def downgrade() -> None:
    with op.batch_alter_table('restore_requests', schema=None) as batch_op:
        batch_op.drop_constraint(
            'fk_restore_requests_destination_connection_id_connections', type_='foreignkey'
        )
        batch_op.drop_column('destination_connection_id')

    with op.batch_alter_table('connections', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_connections_purpose'))
        batch_op.drop_index(batch_op.f('ix_connections_last_health'))
        batch_op.drop_index(batch_op.f('ix_connections_hostname'))
        batch_op.create_index(batch_op.f('ix_sources_last_health'), ['last_health'], unique=False)
        batch_op.create_index(batch_op.f('ix_sources_hostname'), ['hostname'], unique=False)
        batch_op.drop_constraint('uq_connection_hostname_purpose', type_='unique')
        batch_op.create_unique_constraint('uq_source_hostname', ['hostname'])
        batch_op.drop_column('purpose')

    op.rename_table('connections', 'sources')
