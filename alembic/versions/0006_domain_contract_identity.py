"""consolidate and constrain canonical domain contract identities

Revision ID: 0006
Revises: 0005
"""

import hashlib
import json
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "uq_domain_contracts_market_external_id_domain"


def _contract_fingerprint(market_id: str, domain: str) -> str:
    encoded = json.dumps(
        {"market_id": market_id, "domain": domain},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def upgrade() -> None:
    connection = op.get_bind()
    contracts = sa.table(
        "domain_contracts",
        sa.column("id", sa.Integer()),
        sa.column("market_external_id", sa.String()),
        sa.column("domain", sa.String()),
        sa.column("fingerprint", sa.String()),
    )
    predictions = sa.table(
        "prediction_runs",
        sa.column("contract_id", sa.Integer()),
    )
    identities = connection.execute(
        sa.select(contracts.c.market_external_id, contracts.c.domain)
        .group_by(contracts.c.market_external_id, contracts.c.domain)
        .order_by(contracts.c.market_external_id, contracts.c.domain)
    ).all()
    for market_id, domain in identities:
        ids = (
            connection.execute(
                sa.select(contracts.c.id)
                .where(
                    contracts.c.market_external_id == market_id,
                    contracts.c.domain == domain,
                )
                .order_by(contracts.c.id.desc())
            )
            .scalars()
            .all()
        )
        keeper_id, *sibling_ids = ids
        if sibling_ids:
            connection.execute(
                predictions.update()
                .where(predictions.c.contract_id.in_(sibling_ids))
                .values(contract_id=keeper_id)
            )
            connection.execute(contracts.delete().where(contracts.c.id.in_(sibling_ids)))
        connection.execute(
            contracts.update()
            .where(contracts.c.id == keeper_id)
            .values(fingerprint=_contract_fingerprint(str(market_id), str(domain)))
        )

    with op.batch_alter_table("domain_contracts") as batch_op:
        batch_op.create_unique_constraint(_CONSTRAINT, ["market_external_id", "domain"])


def downgrade() -> None:
    with op.batch_alter_table("domain_contracts") as batch_op:
        batch_op.drop_constraint(_CONSTRAINT, type_="unique")
    # Consolidation is intentionally irreversible: deleted duplicate audit rows are not recreated.
