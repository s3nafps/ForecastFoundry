import importlib
from pathlib import Path
from typing import Any

from app.services.keystore import KeystoreError, decrypt_keystore


class PolymarketSDKError(RuntimeError):
    pass


def build_official_client(
    host: str,
    *,
    keystore_path: Path,
    password: str,
    chain_id: int = 137,
    funder: str | None = None,
) -> Any:
    try:
        sdk = importlib.import_module("py_clob_client_v2")
    except ImportError as exc:
        raise PolymarketSDKError(
            "install the optional live dependency: pip install forecastfoundry[live]"
        ) from exc
    try:
        payload = decrypt_keystore(keystore_path, password)
        private_key = payload["private_key"]
        if not isinstance(private_key, str) or not private_key:
            raise KeystoreError("private_key is missing from keystore")
        kwargs: dict[str, object] = {"host": host, "chain_id": chain_id, "key": private_key}
        if funder:
            kwargs["funder"] = funder
        credentials = payload.get("api_credentials")
        if isinstance(credentials, dict):
            kwargs["creds"] = sdk.ApiCreds(
                api_key=credentials["api_key"],
                api_secret=credentials["api_secret"],
                api_passphrase=credentials["api_passphrase"],
            )
        return sdk.ClobClient(**kwargs)
    except (KeyError, TypeError, KeystoreError, AttributeError) as exc:
        raise PolymarketSDKError(
            "encrypted keystore does not contain valid SDK credentials"
        ) from exc
