import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.domains.base import MarketInput
from app.domains.weather import WeatherPlugin
from app.models import DomainContract, Signal
from app.services.crypto_data import canonical_payload_hash
from app.services.paper import SettlementEvidence, _derive_settlement_outcome
from app.services.polymarket import parse_gamma_search
from app.services.rules import load_station_registry


def test_strict_weather_contract_dispatches_bucket_label_before_binary_validation() -> None:
    raw = json.loads(
        (Path(__file__).parent / "fixtures" / "london_event.json").read_text(encoding="utf-8")
    )
    event = parse_gamma_search(raw)[0]
    source = "https://aviationweather.gov/api/data/metar?ids=EGLC&format=json"
    route = WeatherPlugin(
        stations=load_station_registry(Path("config/stations.yaml")),
        overrides={
            "775541": {
                "resolution_source": source,
                "station_id": "EGLC",
                "rounding_method": "half_up",
            }
        },
    ).normalize(
        MarketInput(
            market_id="3237364",
            title=event.title,
            description=event.description,
            raw_data={"event": event.model_dump(mode="json")},
        )
    )
    assert route.accepted is True
    assert route.contract is not None
    contract = DomainContract(
        id=1,
        market_external_id="3237364",
        domain="weather",
        accepted=True,
        resolution_source=source,
        expiry=route.contract.expiry,
        contract_data=route.contract.model_dump(mode="json"),
        rejection_reasons=[],
        provenance=route.contract.provenance,
        fingerprint="strict-weather-contract",
    )
    assert contract.expiry is not None
    local_start = datetime(2026, 8, 1, 23, 30, tzinfo=UTC)
    observations = [
        {
            "icaoId": "EGLC",
            "obsTime": int((local_start + timedelta(hours=3 * index)).timestamp()),
            "temp": 24.6 if index == 4 else 20 + index / 10,
            "rawOb": f"EGLC fixture {index}",
            "receiptTime": int((local_start + timedelta(hours=3 * index, minutes=5)).timestamp()),
        }
        for index in range(8)
    ]
    payload = {
        "provider": "aviation_weather",
        "query": {"ids": "EGLC", "format": "json", "date": "20260802"},
        "observations": observations,
    }
    evidence = SettlementEvidence(
        contract_id=1,
        source=source,
        observed_at=contract.expiry,
        retrieved_at=datetime(2026, 8, 2, 23, 5, tzinfo=UTC),
        outcome_label="YES",
        raw_response_hash=canonical_payload_hash(payload),
        raw_payload=payload,
        normalized_values={
            "station_id": "EGLC",
            "source": source,
            "local_date": "2026-08-02",
            "rounded_value": "25",
            "bucket_label": "24Â°C or higher",
        },
    )

    outcome = _derive_settlement_outcome(
        contract, Signal(outcome_label="24Â°C or higher"), evidence
    )

    assert outcome == "YES"
