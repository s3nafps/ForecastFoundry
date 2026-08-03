from pathlib import Path

import pytest

from app.services.keystore import (
    KeystoreError,
    decrypt_keystore,
    encrypt_keystore,
    validate_encrypted_keystore,
)


def test_keystore_round_trip_never_writes_plaintext(tmp_path: Path) -> None:
    path = tmp_path / "wallet.json"
    encrypt_keystore(path, "correct horse", {"private_key": "0xsecret", "funder": "0xabc"})

    raw = path.read_text(encoding="utf-8")
    assert "0xsecret" not in raw
    assert decrypt_keystore(path, "correct horse")["private_key"] == "0xsecret"
    validate_encrypted_keystore(path)


def test_keystore_rejects_wrong_password_and_malformed_file(tmp_path: Path) -> None:
    path = tmp_path / "wallet.json"
    encrypt_keystore(path, "password", {"private_key": "0xsecret"})

    with pytest.raises(KeystoreError, match="decrypt"):
        decrypt_keystore(path, "wrong")
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(KeystoreError, match="encrypted"):
        validate_encrypted_keystore(path)
