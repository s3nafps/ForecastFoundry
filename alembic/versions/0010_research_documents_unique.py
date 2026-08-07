"""Add a unique identity constraint to research_documents.

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-07
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "DELETE FROM research_documents WHERE id NOT IN "
        "(SELECT MIN(id) FROM research_documents "
        "GROUP BY provider, external_id, content_hash)"
    )
    with op.batch_alter_table("research_documents") as batch_op:
        batch_op.create_unique_constraint(
            "uq_research_documents_identity",
            ["provider", "external_id", "content_hash"],
        )


def downgrade() -> None:
    with op.batch_alter_table("research_documents") as batch_op:
        batch_op.drop_constraint("uq_research_documents_identity", type_="unique")
