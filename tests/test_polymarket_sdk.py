import sys
from pathlib import Path
from types import ModuleType

from app.services.keystore import encrypt_keystore
from app.services.polymarket_sdk import build_official_client


def test_sdk_adapter_uses_official_client_and_encrypted_wallet(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "wallet.json"
    encrypt_keystore(
        path,
        "password",
        {
            "private_key": "0xprivate",
            "api_credentials": {
                "api_key": "key",
                "api_secret": "secret",
                "api_passphrase": "pass",
            },
        },
    )
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    class FakeApiCreds:
        def __init__(self, **kwargs: object) -> None:
            captured["creds"] = kwargs

    fake_module = ModuleType("py_clob_client_v2")
    fake_module.ClobClient = FakeClient
    fake_module.ApiCreds = FakeApiCreds
    monkeypatch.setitem(sys.modules, "py_clob_client_v2", fake_module)

    build_official_client(
        "https://clob.polymarket.com",
        keystore_path=path,
        password="password",
        chain_id=137,
    )

    assert captured["host"] == "https://clob.polymarket.com"
    assert captured["key"] == "0xprivate"
    assert captured["chain_id"] == 137
