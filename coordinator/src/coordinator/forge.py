"""Forge service — end-to-end fusion authorization.

Glues:
  - On-chain reads (token ownership, current fusionNonce, token metadata)
  - LLM oracle (FusionOracle.evaluate)
  - ECDSA signer (Signer.sign_fuse)
  - Persistence (fusion_records)

Flow when an agent calls POST /v1/forge/sign:
  1. Verify caller's address owns both tokens (chain read)
  2. Read tokens' (word, power, langId) from chain
  3. Call FusionOracle.evaluate → compatibility, success roll, suggested word
  4. Read current ArdiNFT.fusionNonce()
  5. Sign authorization with that nonce
  6. Persist to fusion_records
  7. Return {evaluation, signature, nonce}
"""
from __future__ import annotations

import logging
import time

from .config import Config
from .db import DB
from .fusion import FusionOracle, FusionResult, _success_rate
from .signer import Signer


def _sr_str(comp: float) -> str:
    """Human-readable success rate for log lines."""
    return f"{_success_rate(comp) * 100:.1f}%"

log = logging.getLogger("ardi.forge")


# Minimal ArdiNFT ABI for the reads we need
NFT_ABI = [
    {
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "name": "ownerOf",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "name": "getInscription",
        "outputs": [
            {
                "components": [
                    {"name": "word", "type": "string"},
                    {"name": "power", "type": "uint16"},
                    {"name": "languageId", "type": "uint8"},
                    {"name": "generation", "type": "uint8"},
                    {"name": "inscriber", "type": "address"},
                    {"name": "timestamp", "type": "uint64"},
                    {"name": "parents", "type": "uint256[]"},
                ],
                "name": "",
                "type": "tuple",
            },
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "fusionNonce",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]


class ForgeError(Exception):
    pass


class Forge:
    def __init__(self, cfg: Config, db: DB, signer: Signer, oracle: FusionOracle):
        self.cfg = cfg
        self.db = db
        self.signer = signer
        self.oracle = oracle
        self._w3 = None
        self._nft = None

    def _w3_lazy(self):
        if self._w3 is None:
            from web3 import Web3

            self._w3 = Web3(Web3.HTTPProvider(self.cfg.chain.rpc_url))
            self._nft = self._w3.eth.contract(
                address=self.cfg.contracts.ardi_nft, abi=NFT_ABI
            )
        return self._w3

    def _read_token(self, token_id: int) -> dict:
        """Returns {owner, word, power, languageId} from chain.

        Raises ForgeError (HTTP 400) if the token doesn't exist or has been
        burned. Without this catch the underlying web3 ContractCustomError
        bubbles up to a 500, which (a) is misleading and (b) bypasses the
        CORS middleware on its way out — browsers see it as 'Failed to
        fetch' instead of a proper API error.
        """
        from web3.exceptions import ContractCustomError, ContractLogicError
        self._w3_lazy()
        try:
            owner = self._nft.functions.ownerOf(token_id).call()
            ins = self._nft.functions.getInscription(token_id).call()
        except (ContractCustomError, ContractLogicError) as e:
            # ERC721NonexistentToken selector = 0x7e273289 — token never
            # minted, or was burned (e.g. fused away).
            raise ForgeError(
                f"tokenId {token_id} doesn't exist on chain (never minted or already burned). "
                f"refresh your vault list."
            ) from e
        return {
            "owner": owner.lower(),
            "word": ins[0],
            "power": int(ins[1]),
            "language_id": int(ins[2]),
            "generation": int(ins[3]),
        }

    def _fusion_nonce(self) -> int:
        self._w3_lazy()
        return int(self._nft.functions.fusionNonce().call())

    async def quote(self, token_a: int, token_b: int, holder: str) -> dict:
        """Read-only ODDS preview — does NOT roll, does NOT reveal the new
        word, does NOT decide success. The dice are thrown only when the
        holder commits via /v1/forge/sign.

        Surfaces enough for an informed bet:
          - compatibility (LLM's tier × subscore → final score)
          - success_rate (P(success) under current curves)
          - power_if_success (deterministic from compat formula)
          - would_burn_on_fail (lower-power tokenId — what you lose if the roll fails)
          - rationale (LLM's why-this-tier explanation)
        """
        a = self._read_token(token_a)
        b = self._read_token(token_b)

        if a["owner"] != holder.lower() or b["owner"] != holder.lower():
            raise ForgeError("holder does not own both tokens")
        if token_a == token_b:
            raise ForgeError("cannot fuse a token with itself")

        potential = await self.oracle.evaluate_potential(
            a["word"], a["language_id"], a["power"],
            b["word"], b["language_id"], b["power"],
        )
        # Failure branch: contract burns the lower-power token.
        burn_id = token_a if a["power"] <= b["power"] else token_b
        burn_word = a["word"] if a["power"] <= b["power"] else b["word"]
        return {
            "tokenIdA": token_a,
            "tokenIdB": token_b,
            "wordA": a["word"], "powerA": a["power"], "langA": a["language_id"],
            "wordB": b["word"], "powerB": b["power"], "langB": b["language_id"],
            "tier": potential.tier,
            "compatibility": potential.compatibility,
            "rationale": potential.rationale,
            "success_rate": potential.success_rate,
            "multiplier": potential.multiplier,
            "power_if_success": potential.power_if_success,
            "would_burn_on_fail_token_id": burn_id,
            "would_burn_on_fail_word": burn_word,
            "cached": potential.cached,
            # Intentionally absent: suggested_word, success, new_power.
            # These are revealed at /v1/forge/sign time, not here.
        }

    async def sign(self, token_a: int, token_b: int, holder: str) -> dict:
        """Commit the dice roll.

        - Verifies ownership.
        - Calls the LLM (cached) for compat + word.
        - **Rolls success at THIS moment** with fresh randomness, salted by
          (holder, tokenA, tokenB, fusionNonce). The same (holder, A, B,
          nonce) tuple gets ONE roll — re-asking returns the same result
          (idempotent per-nonce). After on-chain `fuse()` increments the
          nonce, a fresh sign on the same pair gets a fresh roll.
        """
        # 1. Verify ownership + read metadata
        a = self._read_token(token_a)
        b = self._read_token(token_b)
        if a["owner"] != holder.lower() or b["owner"] != holder.lower():
            raise ForgeError("holder does not own both tokens")
        if token_a == token_b:
            raise ForgeError("cannot fuse a token with itself")

        # 2. LLM eval (cached) — gives us compat + (potential) suggested word.
        #    NOTE: this still uses the cached deterministic word. Suspense
        #    is preserved by hiding it from /quote. /sign reveals it because
        #    the holder is committing here.
        result: FusionResult = await self.oracle.evaluate(
            a["word"], a["language_id"], a["power"],
            b["word"], b["language_id"], b["power"],
        )

        # 3. Read current fusionNonce — gates idempotency.
        nonce = self._fusion_nonce()

        # 4. Idempotent guard: have we already rolled for this exact tuple?
        #    Same (holder, A, B, nonce) → return the same locked result.
        existing = self._lookup_pending_record(holder, token_a, token_b, nonce)
        if existing is not None:
            log.info(
                f"forge sign: {token_a}+{token_b} → returning locked roll "
                f"(holder={holder} nonce={nonce})"
            )
            return existing

        # 5. THE DICE ROLL — fresh randomness, salted to this attempt
        salt = (
            holder.lower().encode()
            + token_a.to_bytes(32, "big")
            + token_b.to_bytes(32, "big")
            + nonce.to_bytes(32, "big")
        )
        success, new_power = self.oracle.roll_outcome(
            a["word"], a["language_id"], a["power"],
            b["word"], b["language_id"], b["power"],
            suggested_word=result.suggested_word,
            suggested_language_id=result.suggested_language_id,
            compatibility=result.compatibility,
            salt=salt,
        )

        # 6. Sign — failure branch zeroes the new-* fields per contract semantics
        new_word = result.suggested_word if success else ""
        new_lang = result.suggested_language_id if success else 0
        sig = self.signer.sign_fuse(
            chain_id=self.cfg.chain.chain_id,
            contract=self.cfg.contracts.ardi_nft,
            holder=holder,
            token_a=token_a,
            token_b=token_b,
            new_word=new_word,
            new_power=new_power,
            new_lang_id=new_lang,
            success=success,
            nonce=nonce,
        )

        # 7. Persist — used both for indexer enrichment and idempotent re-sign
        response = {
            "tokenIdA": token_a,
            "tokenIdB": token_b,
            "wordA": a["word"], "powerA": a["power"], "langA": a["language_id"],
            "wordB": b["word"], "powerB": b["power"], "langB": b["language_id"],
            "newWord": new_word,
            "newPower": new_power,
            "newLanguageId": new_lang,
            "success": success,
            "compatibility": result.compatibility,
            "rationale": result.rationale,
            "nonce": nonce,
            "signature": "0x" + sig.hex(),
        }
        with self.db.conn() as c:
            c.execute(
                "INSERT INTO fusion_records "
                "(holder, token_a, token_b, word_a, word_b, success, new_token, "
                "new_word, new_power, new_lang, compatibility, rationale, "
                "burned_id, timestamp, tx_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, NULL, ?, NULL)",
                (
                    holder.lower(), token_a, token_b,
                    a["word"], b["word"],
                    1 if success else 0,
                    new_word, new_power, new_lang,
                    float(result.compatibility), result.rationale,
                    int(time.time()),
                ),
            )

        log.info(
            f"forge sign: {token_a}+{token_b} → "
            f"{'SUCCESS ' + new_word if success else 'FAIL burn lower'} "
            f"compat={result.compatibility:.2f} sr={_sr_str(result.compatibility)} nonce={nonce}"
        )

        return response

    def _lookup_pending_record(
        self, holder: str, token_a: int, token_b: int, nonce: int
    ) -> dict | None:
        """Return the previously-signed payload for (holder, A, B, nonce)
        if one exists in fusion_records. Used to make /sign idempotent so a
        holder can't re-roll a bad outcome by re-calling sign before fuse.

        Returns None if no record (caller will roll fresh).
        """
        with self.db.conn() as c:
            row = c.execute(
                "SELECT word_a, word_b, success, new_word, new_power, new_lang, "
                "compatibility, rationale FROM fusion_records "
                "WHERE holder = ? AND token_a = ? AND token_b = ? "
                "  AND tx_hash IS NULL "
                "ORDER BY timestamp DESC LIMIT 1",
                (holder.lower(), token_a, token_b),
            ).fetchone()
        if not row:
            return None
        # Re-sign with the same parameters — signature is determined by
        # (chainId, contract, holder, A, B, newWord, newPower, newLang, success, nonce).
        # All inputs identical → identical signature, no new randomness.
        a = self._read_token(token_a)
        b = self._read_token(token_b)
        sig = self.signer.sign_fuse(
            chain_id=self.cfg.chain.chain_id,
            contract=self.cfg.contracts.ardi_nft,
            holder=holder,
            token_a=token_a,
            token_b=token_b,
            new_word=row["new_word"] or "",
            new_power=int(row["new_power"] or 0),
            new_lang_id=int(row["new_lang"] or 0),
            success=bool(row["success"]),
            nonce=nonce,
        )
        return {
            "tokenIdA": token_a,
            "tokenIdB": token_b,
            "wordA": a["word"], "powerA": a["power"], "langA": a["language_id"],
            "wordB": b["word"], "powerB": b["power"], "langB": b["language_id"],
            "newWord": row["new_word"] or "",
            "newPower": int(row["new_power"] or 0),
            "newLanguageId": int(row["new_lang"] or 0),
            "success": bool(row["success"]),
            "compatibility": float(row["compatibility"] or 0.0),
            "rationale": row["rationale"] or "",
            "nonce": nonce,
            "signature": "0x" + sig.hex(),
        }
