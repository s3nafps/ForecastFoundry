from collections.abc import Mapping
from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class MarketInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market_id: str
    title: str
    description: str = ""
    raw_data: Mapping[str, object] = Field(default_factory=dict)


class NormalizedMarket(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market_id: str
    domain: str
    resolution_source: str
    time_semantics: str
    unit_or_quote: str
    expiry: datetime | None = None
    provenance: dict[str, str]
    ambiguities: tuple[str, ...] = ()


class DomainRoute(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accepted: bool
    domain: str | None = None
    contract: NormalizedMarket | None = None
    reasons: tuple[str, ...] = ()


class DomainPlugin(Protocol):
    name: str

    def matches(self, market: MarketInput) -> bool: ...

    def normalize(self, market: MarketInput) -> DomainRoute: ...
