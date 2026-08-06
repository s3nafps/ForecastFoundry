from collections.abc import Mapping, Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ResearchDocument
from app.providers.research import sanitize_research_text
from app.services.crypto_data import canonical_payload_hash
from app.services.http import ResilientHttpClient


class ResearchIngestError(ValueError):
    pass


def parse_github_issue(
    raw: Mapping[str, object], *, retrieved_at: datetime
) -> ResearchDocument:
    number = raw.get("number")
    title = raw.get("title")
    if number is None or title is None:
        raise ResearchIngestError("GitHub issue is missing number or title")
    user = raw.get("user")
    author = str(user.get("login")) if isinstance(user, Mapping) else ""
    created = raw.get("created_at")
    if isinstance(created, str):
        published_at = datetime.fromisoformat(created.replace("Z", "+00:00"))
    else:
        published_at = None
    body = raw.get("body")
    body_text = body if isinstance(body, str) else ""
    redacted = sanitize_research_text(f"{title}\n\n{body_text}")
    return ResearchDocument(
        provider="github",
        external_id=str(number),
        url=str(raw.get("html_url") or ""),
        published_at=published_at,
        retrieved_at=retrieved_at,
        content_hash=canonical_payload_hash(raw),
        redacted_text=redacted,
        feature_only=True,
        metadata_json={"author": author, "title": str(title)},
    )


async def ingest_github_issues(
    session: AsyncSession,
    payload: Mapping[str, object],
    *,
    retrieved_at: datetime,
) -> int:
    items = payload.get("items")
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        raise ResearchIngestError("GitHub search response must contain items")
    inserted = 0
    for raw in items:
        if not isinstance(raw, Mapping):
            continue
        document = parse_github_issue(raw, retrieved_at=retrieved_at)
        exists = await session.scalar(
            select(ResearchDocument).where(
                ResearchDocument.provider == "github",
                ResearchDocument.external_id == document.external_id,
                ResearchDocument.content_hash == document.content_hash,
            )
        )
        if exists is not None:
            continue
        session.add(document)
        inserted += 1
    await session.commit()
    return inserted


async def fetch_github_issues(
    http: ResilientHttpClient, *, repo: str, since: datetime
) -> Mapping[str, object]:
    payload = await http.request_json(
        "GET",
        "https://api.github.com/search/issues",
        params={
            "q": f"repo:{repo} is:issue updated:>{since.isoformat()}",
            "per_page": 50,
        },
        headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
    )
    if not isinstance(payload, Mapping):
        raise ResearchIngestError("GitHub search response must be an object")
    return payload
