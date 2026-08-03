import pytest

from app.services.geoblock import GeoblockError, assert_geoblock_allows, check_geoblock


class FakeHttp:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    async def request_json(self, method: str, url: str) -> object:
        assert method == "GET"
        assert url.endswith("/geoblock")
        return self.payload


@pytest.mark.asyncio
async def test_geoblock_allows_explicitly_unblocked_response() -> None:
    status = await check_geoblock(FakeHttp({"blocked": False}), "https://example/geoblock")

    assert status.allowed is True


@pytest.mark.asyncio
async def test_geoblock_fails_closed_for_blocked_or_invalid_response() -> None:
    with pytest.raises(GeoblockError, match="blocked"):
        await assert_geoblock_allows(FakeHttp({"blocked": True}), "https://example/geoblock")
    with pytest.raises(GeoblockError, match="invalid"):
        await check_geoblock(FakeHttp({}), "https://example/geoblock")
