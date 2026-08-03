import hashlib
import json

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.base import DomainRoute, MarketInput
from app.models import DomainContract


def contract_fingerprint(
    *,
    market_id: str,
    domain: str,
) -> str:
    encoded = json.dumps(
        {"market_id": market_id, "domain": domain},
        sort_keys=True,
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
    )
    existing = await session.scalar(
        select(DomainContract).where(
            (DomainContract.fingerprint == fingerprint)
            | (
                (DomainContract.market_external_id == market.market_id)
                & (DomainContract.domain == domain)
            )
        )
    )
    if existing is None:
        candidate = DomainContract(
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
        try:
            async with session.begin_nested():
                session.add(candidate)
                await session.flush()
            existing = candidate
        except IntegrityError:
            existing = await session.scalar(
                select(DomainContract).where(DomainContract.fingerprint == fingerprint)
            )
            if existing is None:
                raise

    existing.market_external_id = market.market_id
    existing.domain = domain
    existing.fingerprint = fingerprint
    existing.accepted = route.accepted
    existing.rejection_reasons = list(route.reasons)
    if contract is not None:
        existing.resolution_source = contract.resolution_source
        existing.expiry = contract.expiry
        existing.contract_data = contract_data
        existing.provenance = contract.provenance
    return existing
