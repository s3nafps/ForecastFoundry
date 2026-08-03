"""Persisted BTC/ETH evidence, prediction, and paper-signal path."""

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol, cast

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.domains.base import DomainRoute, MarketInput
from app.domains.crypto import CryptoContract, normalize_named_resolution_source
from app.domains.registry import DomainRegistry
from app.models import (
    DomainContract,
    EvidenceSnapshot,
    PredictionFeature,
    PredictionRun,
    RejectedSignal,
    Signal,
)
from app.providers.registry import ProviderRegistry
from app.schemas import GammaMarket, OrderBook
from app.services.contracts import persist_domain_contract
from app.services.crypto_data import CryptoDataQualityError, CryptoSeries
from app.services.crypto_probability import (
    Comparison,
    ProbabilityEstimate,
    estimate_crypto_probability,
)
from app.services.paper import PaperLifecycle

_MAX_HORIZON = 24 * 31
_PUBLIC_SOURCES = {"coinbase", "binance", "kraken"}


class CryptoDataReader(Protocol):
    async def fetch_series(
        self,
        source: str,
        *,
        asset: str,
        quote: str,
        now: datetime,
        granularity: str = "1h",
        limit: int = 200,
    ) -> CryptoSeries: ...


class OrderBookReader(Protocol):
    async def get_order_books(self, token_ids: Sequence[str]) -> tuple[OrderBook, ...]: ...


class CryptoPaperPipeline:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        data: CryptoDataReader,
        *,
        pricing: OrderBookReader | None = None,
        settings: Settings | None = None,
        registry: DomainRegistry | None = None,
        providers: ProviderRegistry | None = None,
    ) -> None:
        self.sessions = sessions
        self.data = data
        self.pricing = pricing
        self.settings = settings or Settings()
        self.registry = registry or DomainRegistry()
        self.providers = providers or ProviderRegistry.default()

    async def run(
        self,
        market: MarketInput,
        *,
        now: datetime | None = None,
        seed: int = 17,
        samples: int = 2_000,
    ) -> dict[str, object]:
        if now is not None and now.tzinfo is None:
            raise ValueError("now_must_be_timezone_aware")
        captured_at = (now or datetime.now(UTC)).astimezone(UTC)
        route = self.registry.route(market)
        if not route.accepted or not isinstance(route.contract, CryptoContract):
            return await self._reject_contract(market, route, captured_at, route.reasons)
        contract = route.contract
        if contract.source not in _PUBLIC_SOURCES:
            rejected_route = route.model_copy(
                update={"accepted": False, "reasons": ("unsupported_public_source",)}
            )
            return await self._reject_contract(
                market, rejected_route, captured_at, ("unsupported_public_source",)
            )
        if contract.expiry is None or contract.expiry <= captured_at:
            return await self._reject_contract(market, route, captured_at, ("expired_contract",))
        try:
            gamma, outcomes = _gamma_market(market, contract)
        except ValueError as exc:
            return await self._reject_contract(market, route, captured_at, (str(exc),))

        try:
            series = await self.data.fetch_series(
                contract.source,
                asset=contract.asset,
                quote=contract.quote,
                now=captured_at,
            )
        except CryptoDataQualityError as exc:
            return await self._reject_contract(market, route, captured_at, (str(exc),))
        except ValueError as exc:
            reason = str(exc) if str(exc) else "market_data_invalid"
            return await self._reject_contract(market, route, captured_at, (reason,))
        except Exception:
            return await self._reject_contract(
                market, route, captured_at, ("market_data_unavailable",)
            )

        try:
            horizon = _horizon(contract.expiry, series)
        except ValueError as exc:
            return await self._reject_contract(market, route, captured_at, (str(exc),))

        current_price = series.candles[-1].close
        threshold = (
            contract.threshold
            if contract.threshold is not None
            else contract.comparison_reference_price
        )
        estimate = estimate_crypto_probability(
            series.log_returns,
            current_price=current_price,
            comparison=cast(Comparison, contract.comparison),
            threshold=threshold,
            comparison_inclusive=contract.comparison_inclusive,
            rounding_increment=contract.rounding_increment,
            horizon=horizon,
            seed=seed,
            samples=samples,
        )
        evidence_values = _evidence_values(market, contract, gamma, series, self.settings)
        evidence_fingerprint = _fingerprint(
            {
                "contract": market.market_id,
                "provider": series.source,
                "response": series.raw_response_hash,
                "values": evidence_values,
            }
        )
        input_hash = _fingerprint(
            {
                "contract": contract.model_dump(mode="json"),
                "evidence_fingerprint": evidence_fingerprint,
                "model_input_hash": estimate.input_hash,
                "horizon": horizon,
                "seed": seed,
                "samples": samples,
            }
        )

        async with self.sessions() as session:
            domain_contract = await persist_domain_contract(session, market, route)
            evidence = await self._evidence(
                session,
                domain_contract,
                series,
                evidence_values,
                evidence_fingerprint,
            )
            prediction = await self._prediction(
                session,
                domain_contract,
                evidence,
                estimate,
                input_hash,
                horizon,
                seed,
                samples,
                current_price,
            )
            await session.commit()

        policy = _pricing_policy(self.settings)
        if self.pricing is None:
            return await self._reject_signal(
                market,
                domain_contract,
                prediction,
                evidence,
                captured_at,
                ("pricing_unavailable",),
                fingerprint_seed={"input_hash": input_hash, "policy": policy},
                gamma=gamma,
                policy=policy,
            )
        books: tuple[OrderBook, ...] = ()
        try:
            books = await self.pricing.get_order_books((outcomes["YES"][1], outcomes["NO"][1]))
            books_by_outcome = _match_books(gamma, outcomes, books)
        except Exception as exc:
            reason = (
                str(exc) if isinstance(exc, ValueError) and str(exc) else "order_book_unavailable"
            )
            return await self._reject_signal(
                market,
                domain_contract,
                prediction,
                evidence,
                captured_at,
                (reason,),
                fingerprint_seed={
                    "input_hash": input_hash,
                    "policy": policy,
                    "books": [_fingerprint(book.raw_data) for book in books],
                },
                gamma=gamma,
                books={book.asset_id: book for book in books},
                returned_books=books,
                policy=policy,
            )

        candidates = tuple(
            _side_candidate(
                outcome,
                token_id,
                estimate.probability if outcome == "YES" else Decimal("1") - estimate.probability,
                books_by_outcome[outcome],
                estimate,
                self.settings,
                captured_at,
                gamma.minimum_order_size,
            )
            for outcome, (_, token_id) in outcomes.items()
        )
        chosen = max(candidates, key=_candidate_rank)
        reasons = _signal_reasons(gamma, books_by_outcome, chosen, self.settings, captured_at)
        fingerprint_seed = {
            "input_hash": input_hash,
            "books": {
                outcome: _fingerprint(book.raw_data) for outcome, book in books_by_outcome.items()
            },
            "outcome": chosen["outcome"],
            "policy": policy,
        }
        if reasons:
            return await self._reject_signal(
                market,
                domain_contract,
                prediction,
                evidence,
                captured_at,
                reasons,
                fingerprint_seed=fingerprint_seed,
                candidate=chosen,
                gamma=gamma,
                books=books_by_outcome,
                policy=policy,
            )
        return await self._accept_signal(
            market,
            domain_contract,
            prediction,
            evidence,
            captured_at,
            chosen,
            gamma,
            books_by_outcome,
            fingerprint_seed,
            policy,
        )

    async def _reject_contract(
        self,
        market: MarketInput,
        route: DomainRoute,
        captured_at: datetime,
        reasons: Sequence[str],
    ) -> dict[str, object]:
        normalized_reasons = tuple(dict.fromkeys(reasons))
        fingerprint = _fingerprint(
            {
                "kind": "crypto_contract_rejection",
                "market_id": market.market_id,
                "reasons": normalized_reasons,
            }
        )
        async with self.sessions() as session:
            contract = await persist_domain_contract(session, market, route)
            existing = await session.scalar(
                select(RejectedSignal).where(RejectedSignal.fingerprint == fingerprint)
            )
            if existing is None:
                candidate = RejectedSignal(
                    contract_id=contract.id,
                    fingerprint=fingerprint,
                    generated_at=captured_at,
                    reasons=list(normalized_reasons),
                    candidate_data={"market_id": market.market_id},
                )
                try:
                    async with session.begin_nested():
                        session.add(candidate)
                        await session.flush()
                except IntegrityError:
                    existing = await session.scalar(
                        select(RejectedSignal).where(RejectedSignal.fingerprint == fingerprint)
                    )
                    if existing is None:
                        raise
            await session.commit()
        return {
            "market_id": market.market_id,
            "status": "rejected",
            "reasons": list(normalized_reasons),
        }

    async def _evidence(
        self,
        session: AsyncSession,
        contract: DomainContract,
        series: CryptoSeries,
        values: dict[str, object],
        fingerprint: str,
    ) -> EvidenceSnapshot:
        existing = await session.scalar(
            select(EvidenceSnapshot).where(EvidenceSnapshot.fingerprint == fingerprint)
        )
        if existing is not None:
            return existing
        spec = self.providers.get(series.source)
        evidence = EvidenceSnapshot(
            contract_id=contract.id,
            fingerprint=fingerprint,
            provider=series.source,
            provider_version=series.provider_version,
            source_timestamp=series.latest_at,
            retrieved_at=series.retrieved_at,
            raw_response_hash=series.raw_response_hash,
            normalized_values=values,
            quality_flags=list(series.quality_flags),
            freshness_seconds=max(0, int((series.retrieved_at - series.latest_at).total_seconds())),
            license_metadata={
                "attribution": spec.attribution,
                "provider_classification": spec.classification,
                "evidence_role": "authoritative_resolution",
                "named_resolution_source": series.source,
            },
        )
        try:
            async with session.begin_nested():
                session.add(evidence)
                await session.flush()
        except IntegrityError:
            existing = await session.scalar(
                select(EvidenceSnapshot).where(EvidenceSnapshot.fingerprint == fingerprint)
            )
            if existing is None:
                raise
            return cast(EvidenceSnapshot, existing)
        return evidence

    async def _prediction(
        self,
        session: AsyncSession,
        contract: DomainContract,
        evidence: EvidenceSnapshot,
        estimate: ProbabilityEstimate,
        input_hash: str,
        horizon: int,
        seed: int,
        samples: int,
        current_price: Decimal,
    ) -> PredictionRun:
        existing = await session.scalar(
            select(PredictionRun).where(
                PredictionRun.contract_id == contract.id,
                PredictionRun.input_hash == input_hash,
            )
        )
        if existing is not None:
            return existing
        prediction = PredictionRun(
            contract_id=contract.id,
            evidence_snapshot_id=evidence.id,
            generated_at=evidence.retrieved_at,
            model_name=estimate.model_version,
            model_version=estimate.model_version,
            input_hash=input_hash,
            parameters={
                "seed": seed,
                "samples": samples,
                "horizon": horizon,
                "interval_seconds": evidence.normalized_values["interval_seconds"],
                "zero_drift": True,
                "weighting": "equal",
                "comparison_inclusive": estimate.comparison_inclusive,
                "rounding_increment": str(estimate.rounding_increment),
                "rounding_mode": estimate.rounding_mode,
            },
            probabilities={
                "event": str(estimate.probability),
                "bootstrap": str(estimate.bootstrap_probability),
                "monte_carlo": str(estimate.monte_carlo_probability),
            },
            uncertainty="bootstrap_monte_carlo_disagreement",
            status="signal_candidate",
        )
        try:
            async with session.begin_nested():
                session.add(prediction)
                await session.flush()
        except IntegrityError:
            existing = await session.scalar(
                select(PredictionRun).where(
                    PredictionRun.contract_id == contract.id,
                    PredictionRun.input_hash == input_hash,
                )
            )
            if existing is None:
                raise
            return cast(PredictionRun, existing)
        session.add_all(
            (
                PredictionFeature(
                    prediction_run_id=prediction.id,
                    name="current_price",
                    value=str(current_price),
                    source=evidence.provider,
                    live_eligible=True,
                ),
                PredictionFeature(
                    prediction_run_id=prediction.id,
                    name="log_returns",
                    value=evidence.normalized_values["log_returns"],
                    source=evidence.provider,
                    live_eligible=True,
                ),
            )
        )
        return prediction

    async def _accept_signal(
        self,
        market: MarketInput,
        contract: DomainContract,
        prediction: PredictionRun,
        evidence: EvidenceSnapshot,
        captured_at: datetime,
        candidate: dict[str, object],
        gamma: GammaMarket,
        books: dict[str, OrderBook],
        fingerprint_seed: dict[str, object],
        policy: dict[str, str],
    ) -> dict[str, object]:
        fingerprint = _fingerprint({"kind": "crypto_signal", **fingerprint_seed})
        signal_id: int
        async with self.sessions() as session:
            existing = await session.scalar(select(Signal).where(Signal.fingerprint == fingerprint))
            if existing is None:
                existing_id: int | None = None
                signal = Signal(
                    contract_id=contract.id,
                    prediction_run_id=prediction.id,
                    evidence_snapshot_id=evidence.id,
                    side="buy",
                    outcome_label=cast(str, candidate["outcome"]),
                    token_id=cast(str, candidate["token_id"]),
                    generated_at=captured_at,
                    model_probability=cast(Decimal, candidate["probability"]),
                    executable_ask=cast(Decimal, candidate["executable_ask"]),
                    raw_edge=cast(Decimal, candidate["raw_edge"]),
                    usable_edge=cast(Decimal, candidate["usable_edge"]),
                    buffers=cast(dict[str, str], candidate["buffers"]),
                    fingerprint=fingerprint,
                    freshness_seconds=cast(int, candidate["freshness_seconds"]),
                    signal_data=_candidate_data(gamma, books, candidate, policy, self.settings),
                )
                try:
                    async with session.begin_nested():
                        session.add(signal)
                        await session.flush()
                except IntegrityError:
                    existing = await session.scalar(
                        select(Signal).where(Signal.fingerprint == fingerprint)
                    )
                    if existing is None:
                        raise
                    existing_id = existing.id
                await session.commit()
                signal_id = existing_id if existing_id is not None else signal.id
            else:
                signal_id = existing.id
                await session.rollback()
        paper = await PaperLifecycle(self.sessions, self.settings).execute_signal(signal_id)
        return {
            "market_id": market.market_id,
            "status": "accepted",
            "side": "buy",
            "outcome": candidate["outcome"],
            "probability": str(candidate["probability"]),
            "executable_price": str(candidate["executable_ask"]),
            "raw_edge": str(candidate["raw_edge"]),
            "usable_edge": str(candidate["usable_edge"]),
            "fingerprint": fingerprint,
            "paper": paper,
        }

    async def _reject_signal(
        self,
        market: MarketInput,
        contract: DomainContract,
        prediction: PredictionRun,
        evidence: EvidenceSnapshot,
        captured_at: datetime,
        reasons: Sequence[str],
        *,
        fingerprint_seed: dict[str, object],
        candidate: dict[str, object] | None = None,
        gamma: GammaMarket | None = None,
        books: dict[str, OrderBook] | None = None,
        returned_books: Sequence[OrderBook] | None = None,
        policy: dict[str, str] | None = None,
    ) -> dict[str, object]:
        normalized_reasons = tuple(dict.fromkeys(reasons))
        fingerprint = _fingerprint(
            {
                "kind": "crypto_signal_rejection",
                **fingerprint_seed,
                "reasons": normalized_reasons,
            }
        )
        async with self.sessions() as session:
            existing = await session.scalar(
                select(RejectedSignal).where(RejectedSignal.fingerprint == fingerprint)
            )
            if existing is None:
                rejected = RejectedSignal(
                    contract_id=contract.id,
                    prediction_run_id=prediction.id,
                    evidence_snapshot_id=evidence.id,
                    fingerprint=fingerprint,
                    side="buy" if candidate else None,
                    outcome_label=cast(str, candidate["outcome"]) if candidate else None,
                    token_id=cast(str, candidate["token_id"]) if candidate else None,
                    model_probability=(
                        cast(Decimal, candidate["probability"]) if candidate else None
                    ),
                    executable_ask=(
                        cast(Decimal, candidate["executable_ask"]) if candidate else None
                    ),
                    raw_edge=cast(Decimal, candidate["raw_edge"]) if candidate else None,
                    usable_edge=(cast(Decimal, candidate["usable_edge"]) if candidate else None),
                    buffers=(cast(dict[str, str], candidate["buffers"]) if candidate else None),
                    freshness_seconds=(
                        cast(int, candidate["freshness_seconds"]) if candidate else None
                    ),
                    generated_at=captured_at,
                    reasons=list(normalized_reasons),
                    candidate_data=(
                        _candidate_data(
                            gamma,
                            books or {},
                            candidate,
                            policy or {},
                            self.settings,
                        )
                        if candidate and gamma
                        else _rejected_pricing_data(
                            market.market_id,
                            gamma,
                            returned_books or tuple((books or {}).values()),
                            policy or {},
                            self.settings,
                        )
                    ),
                )
                try:
                    async with session.begin_nested():
                        session.add(rejected)
                        await session.flush()
                except IntegrityError:
                    existing = await session.scalar(
                        select(RejectedSignal).where(RejectedSignal.fingerprint == fingerprint)
                    )
                    if existing is None:
                        raise
                await session.commit()
            else:
                await session.rollback()
        return {
            "market_id": market.market_id,
            "status": "rejected",
            "reasons": list(normalized_reasons),
            "side": "buy" if candidate else None,
            "outcome": candidate["outcome"] if candidate else None,
            "fingerprint": fingerprint,
        }


def _gamma_market(
    market: MarketInput, contract: CryptoContract
) -> tuple[GammaMarket, dict[str, tuple[str, str]]]:
    raw = market.raw_data.get("market")
    if not isinstance(raw, dict):
        raise ValueError("market_metadata_missing")
    gamma = GammaMarket.model_validate(raw)
    if gamma.id != market.market_id or gamma.end_date != contract.expiry:
        raise ValueError("market_metadata_mismatch")
    try:
        gamma_source = normalize_named_resolution_source(gamma.resolution_source)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    if gamma_source != contract.source:
        raise ValueError("resolution_source_mismatch")
    if len(gamma.outcomes) != 2 or len(gamma.token_ids) != 2:
        raise ValueError("binary_outcomes_required")
    if len(set(gamma.token_ids)) != len(gamma.token_ids):
        raise ValueError("yes_no_outcome_mapping_invalid")
    if gamma.minimum_order_size is None or gamma.minimum_order_size <= 0:
        raise ValueError("minimum_order_size_missing")
    outcomes: dict[str, tuple[str, str]] = {}
    for label, token_id in zip(gamma.outcomes, gamma.token_ids, strict=True):
        normalized = label.strip().upper()
        if normalized not in {"YES", "NO"} or normalized in outcomes or not token_id:
            raise ValueError("yes_no_outcome_mapping_invalid")
        outcomes[normalized] = (label, token_id)
    if set(outcomes) != {"YES", "NO"}:
        raise ValueError("yes_no_outcome_mapping_invalid")
    return gamma, outcomes


def _horizon(expiry: datetime, series: CryptoSeries) -> int:
    interval = series.interval_seconds
    seconds = int((expiry - series.latest_at).total_seconds())
    if seconds <= 0 or seconds % interval or seconds // interval > _MAX_HORIZON:
        raise ValueError("unsupported_horizon")
    return seconds // interval


def _match_books(
    gamma: GammaMarket,
    outcomes: dict[str, tuple[str, str]],
    books: Sequence[OrderBook],
) -> dict[str, OrderBook]:
    by_token = {book.asset_id: book for book in books}
    expected_tokens = {token_id for _, token_id in outcomes.values()}
    if len(by_token) != len(books) or set(by_token) != expected_tokens:
        raise ValueError("order_book_token_set_mismatch")
    matched: dict[str, OrderBook] = {}
    for outcome, (_, token_id) in outcomes.items():
        book = by_token.get(token_id)
        if (
            book is None
            or not gamma.condition_id
            or not book.condition_id
            or book.condition_id != gamma.condition_id
        ):
            raise ValueError("order_book_mismatch")
        if book.minimum_order_size != gamma.minimum_order_size:
            raise ValueError("minimum_order_size_mismatch")
        _validate_order_book(book)
        matched[outcome] = book
    return matched


def _validate_order_book(book: OrderBook) -> None:
    if (
        book.best_bid is None
        or book.best_ask is None
        or book.midpoint is None
        or book.spread is None
    ):
        raise ValueError("order_book_incomplete")
    if not all(
        Decimal("0") <= value <= Decimal("1")
        for value in (book.best_bid, book.best_ask, book.midpoint, book.spread)
    ):
        raise ValueError("order_book_financial_invariant")
    if book.best_ask < book.best_bid or book.spread != book.best_ask - book.best_bid:
        raise ValueError("order_book_financial_invariant")
    if book.midpoint != (book.best_ask + book.best_bid) / Decimal("2"):
        raise ValueError("order_book_financial_invariant")
    if book.available_depth < 0 or book.minimum_order_size <= 0 or book.tick_size <= 0:
        raise ValueError("order_book_financial_invariant")
    if tuple(sorted(book.asks, key=lambda level: level.price)) != book.asks:
        raise ValueError("order_book_level_order_invalid")
    if tuple(sorted(book.bids, key=lambda level: level.price, reverse=True)) != book.bids:
        raise ValueError("order_book_level_order_invalid")
    if not book.asks or not book.bids:
        raise ValueError("order_book_incomplete")
    if book.best_ask != book.asks[0].price or book.best_bid != book.bids[0].price:
        raise ValueError("order_book_financial_invariant")
    if book.available_depth != sum((level.size for level in book.asks), Decimal("0")):
        raise ValueError("order_book_financial_invariant")
    if any(level.price % book.tick_size for level in (*book.bids, *book.asks)):
        raise ValueError("order_book_tick_mismatch")
    _book_time(book.timestamp)


def _side_candidate(
    outcome: str,
    token_id: str,
    probability: Decimal,
    book: OrderBook,
    estimate: ProbabilityEstimate,
    settings: Settings,
    now: datetime,
    required_size: Decimal | None,
) -> dict[str, object]:
    if required_size is None or required_size <= 0:
        raise ValueError("minimum_order_size_missing")
    executable_ask = _execution_vwap(book, required_size)
    edge_price = executable_ask if executable_ask is not None else cast(Decimal, book.best_ask)
    spread = book.spread if book.spread is not None else Decimal("1")
    liquidity_buffer = (
        min(
            Decimal("1"),
            (book.minimum_order_size / book.available_depth) * book.tick_size,
        )
        if book.available_depth > 0
        else Decimal("1")
    )
    uncertainty = min(
        Decimal("1"),
        settings.uncertainty_buffer
        + abs(estimate.bootstrap_probability - estimate.monte_carlo_probability) / Decimal("2"),
    )
    buffers = {
        "estimated_fee": settings.estimated_fee,
        "slippage": settings.slippage_buffer,
        "uncertainty": uncertainty,
        "rule_risk": settings.rule_risk_buffer,
        "liquidity": liquidity_buffer,
        "spread": spread,
    }
    raw_edge = probability - edge_price
    usable_edge = (
        raw_edge - sum((value for key, value in buffers.items() if key != "spread"), Decimal("0"))
        if executable_ask is not None
        else None
    )
    book_time = _book_time(book.timestamp)
    return {
        "outcome": outcome,
        "token_id": token_id,
        "probability": probability,
        "executable_ask": executable_ask,
        "raw_edge": raw_edge,
        "usable_edge": usable_edge,
        "buffers": {key: str(value) for key, value in buffers.items()},
        "freshness_seconds": max(0, int((now - book_time).total_seconds())),
        "required_size": required_size,
        "fully_executable": executable_ask is not None,
    }


def _execution_vwap(book: OrderBook, required_size: Decimal) -> Decimal | None:
    remaining = required_size
    total = Decimal("0")
    for level in book.asks:
        filled = min(remaining, level.size)
        total += filled * level.price
        remaining -= filled
        if remaining == 0:
            return total / required_size
    return None


def _candidate_rank(candidate: dict[str, object]) -> Decimal:
    usable = candidate["usable_edge"]
    return cast(Decimal, usable if usable is not None else candidate["raw_edge"])


def _signal_reasons(
    gamma: GammaMarket,
    books: dict[str, OrderBook],
    chosen: dict[str, object],
    settings: Settings,
    now: datetime,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if not gamma.active:
        reasons.append("market_inactive")
    if gamma.closed or gamma.end_date is None or gamma.end_date <= now:
        reasons.append("market_closed")
    if (gamma.liquidity or Decimal("0")) < settings.min_liquidity_usd:
        reasons.append("liquidity_below_minimum")
    for book in books.values():
        if book.spread is None or book.spread > settings.max_spread:
            reasons.append("spread_above_maximum")
        if book.available_depth < book.minimum_order_size:
            reasons.append("insufficient_executable_depth")
        book_time = _book_time(book.timestamp)
        if book_time > now:
            reasons.append("order_book_clock_skew")
        elif now - book_time > timedelta(seconds=settings.polymarket_poll_seconds * 2):
            reasons.append("order_book_stale")
    if not cast(bool, chosen["fully_executable"]):
        reasons.append("insufficient_executable_depth")
    usable_edge = cast(Decimal | None, chosen["usable_edge"])
    if usable_edge is None or usable_edge < settings.min_usable_edge:
        reasons.append("usable_edge_below_minimum")
    return tuple(dict.fromkeys(reasons))


def _book_time(value: str) -> datetime:
    try:
        if value.isdigit():
            divisor = 1000 if len(value) > 10 else 1
            return datetime.fromtimestamp(int(value) / divisor, UTC)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (OverflowError, ValueError) as exc:
        raise ValueError("order_book_timestamp_invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("order_book_timestamp_invalid")
    return parsed.astimezone(UTC)


def _evidence_values(
    market: MarketInput,
    contract: CryptoContract,
    gamma: GammaMarket,
    series: CryptoSeries,
    settings: Settings,
) -> dict[str, object]:
    return {
        "market_id": market.market_id,
        "source": series.source,
        "asset": contract.asset,
        "quote": contract.quote,
        "interval_seconds": series.interval_seconds,
        "candles": [
            {"timestamp": candle.timestamp.isoformat(), "close": str(candle.close)}
            for candle in series.candles
        ],
        "log_returns": [str(value) for value in series.log_returns],
        "latest_price": str(series.candles[-1].close),
        "source_timestamp": series.latest_at.isoformat(),
        "retrieved_at": series.retrieved_at.isoformat(),
        "request": {
            "url": series.request_url,
            "query": {key: series.request_params[key] for key in sorted(series.request_params)},
        },
        "market_acquisition": {
            "provider": "polymarket_gamma",
            "method": "GET",
            "market_url": f"{settings.gamma_api_url.rstrip('/')}/markets/{gamma.id}",
            "market_id": gamma.id,
            "condition_id": gamma.condition_id,
            "resolution_source": gamma.resolution_source,
            "gamma_market_hash": _fingerprint(gamma.model_dump(mode="json")),
            "raw_response": gamma.model_dump(mode="json"),
        },
    }


def _candidate_data(
    gamma: GammaMarket,
    books: dict[str, OrderBook],
    candidate: dict[str, object],
    policy: dict[str, str],
    settings: Settings,
) -> dict[str, object]:
    return {
        "market": {
            "id": gamma.id,
            "active": gamma.active,
            "closed": gamma.closed,
            "close_time": gamma.end_date.isoformat() if gamma.end_date else None,
            "liquidity": str(gamma.liquidity) if gamma.liquidity is not None else None,
        },
        "candidate": {
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in candidate.items()
        },
        "policy": policy,
        "pricing_acquisition": _pricing_acquisition(gamma, settings),
        "order_books": {outcome: _book_record(book) for outcome, book in books.items()},
    }


def _pricing_policy(settings: Settings) -> dict[str, str]:
    return {
        "version": "crypto-signal-v2",
        "required_size_basis": "gamma_clob_minimum_order_size",
        "min_usable_edge": str(settings.min_usable_edge),
        "max_spread": str(settings.max_spread),
        "min_liquidity": str(settings.min_liquidity_usd),
        "estimated_fee": str(settings.estimated_fee),
        "slippage": str(settings.slippage_buffer),
        "uncertainty": str(settings.uncertainty_buffer),
        "rule_risk": str(settings.rule_risk_buffer),
        "book_freshness_seconds": str(settings.polymarket_poll_seconds * 2),
    }


def _rejected_pricing_data(
    market_id: str,
    gamma: GammaMarket | None,
    books: Sequence[OrderBook],
    policy: dict[str, str],
    settings: Settings,
) -> dict[str, object]:
    return {
        "market_id": market_id,
        "condition_id": gamma.condition_id if gamma else None,
        "policy": policy,
        "pricing_acquisition": (
            _pricing_acquisition(gamma, settings)
            if gamma
            else {
                "provider": "polymarket_clob",
                "method": "POST",
                "endpoint": f"{settings.clob_api_url.rstrip('/')}/books",
                "request_payload": [],
            }
        ),
        "returned_books": [_book_record(book) for book in books],
    }


def _pricing_acquisition(gamma: GammaMarket, settings: Settings) -> dict[str, object]:
    tokens_by_outcome = {
        label.strip().upper(): token_id
        for label, token_id in zip(gamma.outcomes, gamma.token_ids, strict=True)
    }
    return {
        "provider": "polymarket_clob",
        "method": "POST",
        "endpoint": f"{settings.clob_api_url.rstrip('/')}/books",
        "request_payload": [{"token_id": tokens_by_outcome[outcome]} for outcome in ("YES", "NO")],
    }


def _book_record(book: OrderBook) -> dict[str, object]:
    return {
        "token_id": book.asset_id,
        "condition_id": book.condition_id,
        "timestamp": book.timestamp,
        "bids": [{"price": str(level.price), "size": str(level.size)} for level in book.bids],
        "asks": [{"price": str(level.price), "size": str(level.size)} for level in book.asks],
        "best_bid": str(book.best_bid) if book.best_bid is not None else None,
        "best_ask": str(book.best_ask) if book.best_ask is not None else None,
        "midpoint": str(book.midpoint) if book.midpoint is not None else None,
        "spread": str(book.spread) if book.spread is not None else None,
        "available_depth": str(book.available_depth),
        "minimum_order_size": str(book.minimum_order_size),
        "tick_size": str(book.tick_size),
        "raw_response_hash": _fingerprint(book.raw_data),
        "raw_response": json.loads(json.dumps(book.raw_data, sort_keys=True, default=str)),
    }


def _fingerprint(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
