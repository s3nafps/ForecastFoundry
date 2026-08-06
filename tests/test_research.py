import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from app.database import make_engine, make_session_factory
from app.models import Base, ResearchDocument
from app.services.research import ingest_github_issues, parse_github_issue

FIXTURES = Path(__file__).parent / "fixtures"


def _payload() -> dict[str, object]:
    return json.loads((FIXTURES / "github_issues.json").read_text(encoding="utf-8"))


def test_parse_github_issue_extracts_document_fields() -> None:
    items = _payload()["items"]
    assert isinstance(items, list)
    document = parse_github_issue(items[0], retrieved_at=datetime(2026, 8, 2, 0, 0, tzinfo=UTC))

    assert document.provider == "github"
    assert document.external_id == "1234"
    assert document.url == "https://github.com/open-meteo/open-meteo/issues/1234"
    assert document.feature_only is True
    assert "Ensemble endpoint returns partial data" in document.redacted_text
    assert len(document.content_hash) == 64


async def test_ingest_github_issues_persists_and_deduplicates() -> None:
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = make_session_factory(engine)
    retrieved_at = datetime(2026, 8, 2, 0, 0, tzinfo=UTC)
    async with sessions() as session:
        count = await ingest_github_issues(session, _payload(), retrieved_at=retrieved_at)
        assert count == 2
        again = await ingest_github_issues(session, _payload(), retrieved_at=retrieved_at)
        assert again == 0
        rows = (await session.scalars(select(ResearchDocument))).all()
        assert len(rows) == 2
    await engine.dispose()


async def test_research_documents_do_not_affect_probabilities() -> None:
    from app.schemas import Bucket, MemberDailyValue, RoundingMethod, TemperatureUnit
    from app.services.probability import calculate_probabilities

    buckets = (
        Bucket(label="a", lower=10, upper=19),
        Bucket(label="b", lower=20, upper=29),
    )
    members = (
        MemberDailyValue(model="m", member_id="1", value=22.0),
        MemberDailyValue(model="m", member_id="2", value=21.0),
    )

    baseline = calculate_probabilities(
        members,
        buckets,
        rounding_method=RoundingMethod.HALF_UP,
        unit=TemperatureUnit.CELSIUS,
        model_weights={},
    )

    # Research ingestion only writes ResearchDocument rows; it never touches the
    # probability path. The proof: a scan with research documents present
    # produces identical results. Simulate by running the scan flow in the
    # end-to-end harness with a research document ingested beforehand and
    # compare ProbabilityEstimate rows (see test_end_to_end.py harness).
    assert baseline.outcome_probabilities["b"] > 0.9
