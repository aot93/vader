"""restore_requests.destination_connection_id: ON DELETE SET NULL

Removing a connection that a past restore request happened to reference
was raising a raw FOREIGN KEY constraint IntegrityError (500) instead of
succeeding — the FK is purely for display/audit (the request's resolved
destination_path is independent of the connection still existing), so a
deleted connection should just null out old requests' reference to it,
not block the delete.

Revision ID: f2e3d4c5b6a7
Revises: c1a2b3d4e5f6
Create Date: 2026-09-16 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f2e3d4c5b6a7'
down_revision: Union[str, None] = 'c1a2b3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_FK_NAME = 'fk_restore_requests_destination_connection_id_connections'


def upgrade() -> None:
    with op.batch_alter_table('restore_requests', schema=None) as batch_op:
        batch_op.drop_constraint(_FK_NAME, type_='foreignkey')
        batch_op.create_foreign_key(
            _FK_NAME, 'connections', ['destination_connection_id'], ['id'],
            ondelete='SET NULL',
        )


def downgrade() -> None:
    with op.batch_alter_table('restore_requests', schema=None) as batch_op:
        batch_op.drop_constraint(_FK_NAME, type_='foreignkey')
        batch_op.create_foreign_key(
            _FK_NAME, 'connections', ['destination_connection_id'], ['id'],
        )
