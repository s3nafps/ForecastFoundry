"""Short-lived, hashed operator capabilities for state-changing controls."""

import hashlib
import hmac
import os
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import OperatorCredential, utcnow

_ALGORITHM = "pbkdf2_sha256"
_ITERATIONS = 310_000
_MAX_FAILURES = 5
_BLOCK_SECONDS = 60


class OperatorAuthError(PermissionError):
    pass


def hash_token(token: str, *, salt: bytes | None = None) -> str:
    if not token or len(token) < 12:
        raise ValueError("operator token must contain at least 12 characters")
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", token.encode(), salt, _ITERATIONS)
    return f"{_ALGORITHM}${_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_hash(token: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = encoded.split("$", 3)
        if algorithm != _ALGORITHM:
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", token.encode(), bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (TypeError, ValueError):
        return False


class OperatorAuth:
    def __init__(self, session: AsyncSession, *, bootstrap_token: str | None = None) -> None:
        self.session = session
        self.bootstrap_token = bootstrap_token or os.getenv("FORECASTFOUNDRY_OPERATOR_TOKEN")

    async def ensure_bootstrap(self) -> None:
        """Hash the opt-in bootstrap token once; never persist the clear value."""
        if not self.bootstrap_token:
            return
        existing = await self.session.scalar(
            select(OperatorCredential).where(OperatorCredential.name == "bootstrap")
        )
        if existing is None:
            self.session.add(
                OperatorCredential(
                    name="bootstrap",
                    token_hash=hash_token(self.bootstrap_token),
                    permissions=["execution:pause", "execution:resume", "execution:read"],
                    expires_at=datetime.now(UTC) + timedelta(days=30),
                )
            )
            await self.session.flush()

    async def authenticate(self, token: str, permission: str) -> OperatorCredential:
        if not token:
            raise OperatorAuthError("operator authentication failed")
        credential = await self.session.scalar(
            select(OperatorCredential).where(OperatorCredential.name == "bootstrap")
        )
        now = datetime.now(UTC)
        if credential is None:
            await self.ensure_bootstrap()
            credential = await self.session.scalar(
                select(OperatorCredential).where(OperatorCredential.name == "bootstrap")
            )
        if credential is None:
            raise OperatorAuthError("operator authentication failed")
        if credential.blocked_until and credential.blocked_until > now:
            raise OperatorAuthError("operator authentication temporarily rate limited")
        valid = (
            credential.revoked_at is None
            and (credential.expires_at is None or credential.expires_at > now)
            and permission in credential.permissions
            and verify_hash(token, credential.token_hash)
        )
        if not valid:
            credential.failed_attempts += 1
            if credential.failed_attempts >= _MAX_FAILURES:
                credential.blocked_until = now + timedelta(seconds=_BLOCK_SECONDS)
                credential.failed_attempts = 0
            await self.session.flush()
            raise OperatorAuthError("operator authentication failed")
        credential.failed_attempts = 0
        credential.blocked_until = None
        await self.session.flush()
        return credential

    async def rotate(
        self,
        *,
        name: str,
        new_token: str,
        permissions: list[str],
        expires_at: datetime | None,
    ) -> None:
        if not permissions or any(not item.startswith("execution:") for item in permissions):
            raise ValueError("operator permissions are restricted to execution capabilities")
        credential = await self.session.scalar(
            select(OperatorCredential).where(OperatorCredential.name == name)
        )
        if credential is None:
            credential = OperatorCredential(
                name=name,
                token_hash=hash_token(new_token),
                permissions=permissions,
                expires_at=expires_at,
            )
            self.session.add(credential)
        else:
            credential.token_hash = hash_token(new_token)
            credential.permissions = permissions
            credential.expires_at = expires_at
            credential.revoked_at = None
            credential.rotated_at = utcnow()
        await self.session.flush()
