import base64
import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class KeystoreError(ValueError):
    pass


_VERSION = 1
_AAD = b"forecastfoundry-keystore-v1"


def encrypt_keystore(path: Path, password: str, payload: dict[str, Any]) -> None:
    if not password:
        raise KeystoreError("password is required")
    if not path.is_absolute():
        raise KeystoreError("keystore path must be absolute")
    path.parent.mkdir(parents=True, exist_ok=True)
    salt = os.urandom(16)
    nonce = os.urandom(12)
    key = _derive_key(password, salt)
    plaintext = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, _AAD)
    envelope = {
        "version": _VERSION,
        "algorithm": "AES-256-GCM",
        "kdf": "scrypt",
        "salt": _encode(salt),
        "nonce": _encode(nonce),
        "ciphertext": _encode(ciphertext),
    }
    path.write_text(json.dumps(envelope, sort_keys=True), encoding="utf-8")
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def decrypt_keystore(path: Path, password: str) -> dict[str, Any]:
    if not password:
        raise KeystoreError("password is required")
    envelope = _read_envelope(path)
    try:
        plaintext = AESGCM(_derive_key(password, _decode(envelope["salt"]))).decrypt(
            _decode(envelope["nonce"]), _decode(envelope["ciphertext"]), _AAD
        )
        payload = json.loads(plaintext)
    except (KeyError, TypeError, ValueError, InvalidTag, json.JSONDecodeError) as exc:
        raise KeystoreError("keystore decrypt failed") from exc
    if not isinstance(payload, dict):
        raise KeystoreError("keystore payload must be an object")
    return payload


def validate_encrypted_keystore(path: Path) -> None:
    _read_envelope(path)


def _read_envelope(path: Path) -> dict[str, object]:
    if not path.is_file() or stat.S_ISREG(path.stat().st_mode) is False:
        raise KeystoreError("encrypted keystore file is missing")
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise KeystoreError("encrypted keystore is invalid") from exc
    if (
        not isinstance(envelope, dict)
        or envelope.get("version") != _VERSION
        or envelope.get("algorithm") != "AES-256-GCM"
        or envelope.get("kdf") != "scrypt"
        or not all(
            isinstance(envelope.get(field), str) for field in ("salt", "nonce", "ciphertext")
        )
    ):
        raise KeystoreError("encrypted keystore envelope is invalid")
    for field in ("salt", "nonce", "ciphertext"):
        _decode(envelope[field])
    return envelope


def _derive_key(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii")


def _decode(value: object) -> bytes:
    if not isinstance(value, str):
        raise KeystoreError("keystore field is invalid")
    return base64.urlsafe_b64decode(value.encode("ascii"))
