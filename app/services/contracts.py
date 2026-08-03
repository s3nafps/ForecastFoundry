import hashlib
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.base import DomainRoute, MarketInput
from app.models import DomainContract


def contract_fingerprint(
    *,
    market_id: str,
    domain: str,
    accepted: bool,
    contract_data: dict[str, object],
    rejection_reasons: tuple[str, ...],
) -> str:
    encoded = json.dumps(
        {
            "market_id": market_id,
            "domain": domain,
            "accepted": accepted,
            "contract": contract_data,
            "rejection_reasons": rejection_reasons,
        },
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


async def persist_domain_contract(
    session: AsyncSession, market: MarketInput, route: DomainRoute
) -> DomainContract:
    contract = route.contract
    domain = contract.domain if contract else (route.domain or "unknown")
    contract_data: dict[str, object]
    if contract:
        contract_data = contract.model_dump(mode="json")
    else:
        contract_data = {"title": market.title, "description": market.description}
    fingerprint = contract_fingerprint(
        market_id=market.market_id,
        domain=domain,
        accepted=route.accepted,
        contract_data=contract_data,
        rejection_reasons=route.reasons,
    )
    existing = await session.scalar(
        select(DomainContract).where(DomainContract.fingerprint == fingerprint)
    )
    if existing is not None:
        return existing
    persisted = DomainContract(
        market_external_id=market.market_id,
        domain=domain,
        accepted=route.accepted,
        resolution_source=contract.resolution_source if contract else None,
        expiry=contract.expiry if contract else None,
        contract_data=contract_data,
        rejection_reasons=list(route.reasons),
        provenance=contract.provenance if contract else {"title": "polymarket"},
        fingerprint=fingerprint,
    )
    session.add(persisted)
    await session.flush()
    return persisted
