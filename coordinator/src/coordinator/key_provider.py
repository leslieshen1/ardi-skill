"""Abstract Coordinator-key provider.

Production deployments should NOT keep the Coordinator's signing key as a
plain env var. This module defines a thin interface (`KeyProvider`) that
any concrete signer (env-var, AWS KMS, GCP KMS, HashiCorp Vault, hardware
HSM) can implement.

The `Signer` class consumes a `KeyProvider`; today the default factory
returns an env-var-backed provider for backwards compatibility, but a
deployment can supply an `AwsKmsKeyProvider` (etc.) without touching
`Signer` itself.

Switching providers requires only:
  signer = Signer(key_provider=AwsKmsKeyProvider("alias/ardi-coord-prod"))

Key rotation:
  All providers expose `address` so Coordinator startup can verify the
  resolved address matches what's set on-chain via setCoordinator. Mismatch
  → log loud, refuse to sign.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from eth_account import Account
from eth_account.messages import encode_defunct


class KeyProvider(ABC):
    """Abstract signer. Implementations may live in-process or call out
    to an HSM / KMS / cloud secret manager."""

    @property
    @abstractmethod
    def address(self) -> str:
        """The signer's Ethereum address (checksum)."""

    @abstractmethod
    def sign_eth_message(self, digest: bytes) -> bytes:
        """Sign `digest` with EIP-191 personal_sign envelope.
        Returns 65-byte (r || s || v) signature.

        The EVM-side verifier wraps the digest in
            keccak256("\\x19Ethereum Signed Message:\\n32" + digest)
        before recovering — implementations MUST match this convention.
        """


class EnvVarKeyProvider(KeyProvider):
    """Reads a hex private key from an env var or directly. Default for dev/test.

    PRODUCTION WARNING: do NOT use in production. The private key is held in
    process memory; any heap dump, core file, or env var leak (logs, error
    pages) exposes it. Use AwsKmsKeyProvider or similar in prod.
    """

    def __init__(self, private_key: str):
        if not private_key:
            raise ValueError("private_key is empty")
        self._account = Account.from_key(private_key)

    @property
    def address(self) -> str:
        return self._account.address

    def sign_eth_message(self, digest: bytes) -> bytes:
        msg = encode_defunct(primitive=digest)
        return self._account.sign_message(msg).signature


class AwsKmsKeyProvider(KeyProvider):
    """Signs via AWS KMS. The KMS key must be a `KEY_TYPE=ECC_SECG_P256K1`
    CMK (the curve Ethereum uses). The pre-derived Ethereum address is
    cached locally; KMS only sees the digest.

    Deploy notes:
      - Operator runs one-time tool to derive the address from the KMS public
        key and stores it in config (so we don't fetch on every startup).
      - IAM policy: only `kms:Sign` + `kms:GetPublicKey`, no Create / Delete.
      - Audit logging: every Sign call is in CloudTrail.

    NOT IMPLEMENTED — stub. Operators wanting AWS KMS today should use
    eth-keyfile + a bare HSM or build this out per their compliance needs.
    """

    def __init__(self, kms_key_id: str, expected_address: str, kms_client: Any | None = None):
        self._kms_key_id = kms_key_id
        self._expected_address = expected_address
        self._client = kms_client  # caller supplies boto3.client('kms') instance

    @property
    def address(self) -> str:
        return self._expected_address

    def sign_eth_message(self, digest: bytes) -> bytes:
        raise NotImplementedError(
            "AwsKmsKeyProvider is a stub — implement using boto3 KMS Sign + "
            "secp256k1 signature DER->RSV conversion. See README key_provider "
            "section for a worked example."
        )


# ----------------------------------------------------------------------------
# Backwards-compat helpers
# ----------------------------------------------------------------------------


def from_config(coord_cfg) -> KeyProvider:
    """Default factory used by main.py — picks the right provider from config.

    For now: env-var only. Future: branch on `coord_cfg.kms_provider` to
    return AwsKmsKeyProvider, GcpKmsKeyProvider, etc. without changing
    callers.
    """
    return EnvVarKeyProvider(coord_cfg.private_key)
