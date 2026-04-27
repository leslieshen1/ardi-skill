"""Ardi Agent SDK — Python helper for participating in the on-chain
commit-reveal lottery and ArdiNFT mint/fuse flow.

Designed for AI agents and human operators to interact with the protocol
without re-implementing all the on-chain plumbing. Wraps web3.py with
typed helpers, sensible defaults, and the exact commit-hash format the
ArdiEpochDraw contract expects.

Quick start:

    from ardi_sdk import ArdiClient

    client = ArdiClient(
        rpc_url="https://mainnet.base.org",
        coordinator_url="https://api.ardi.work",
        agent_private_key=os.environ["AGENT_PK"],
        contracts={
            "ardi_nft": "0x...",
            "ardi_token": "0x...",
            "bond_escrow": "0x...",
            "epoch_draw": "0x...",
            "mint_controller": "0x...",
            "mock_awp": "0x...",
        },
    )

    # 1. (one-time) register as a miner — needs 10K AWP + KYA
    client.register_miner()

    # 2. Per epoch: fetch riddles, solve, commit, then reveal
    epoch = client.fetch_current_epoch()
    for riddle in epoch.riddles[:3]:
        guess = my_solver.solve(riddle.riddle)  # bring your own LLM
        nonce = client.commit(epoch.epoch_id, riddle.word_id, guess)

    # 3. Wait for commit window to close (epoch.commit_deadline timestamp)
    # 4. After Coordinator publishes answers, reveal what you committed
    client.reveal(epoch.epoch_id, word_id, guess, nonce)

    # 5. After reveal window + VRF, check if you won
    winner = client.winner_of(epoch.epoch_id, word_id)
    if winner == client.address:
        client.inscribe(epoch.epoch_id, word_id)

The SDK is stateless except for the agent's private key + nonces.
Agents that want to recover state across restarts should persist
their (epoch_id, word_id, guess, nonce) tuples themselves until
reveal completes.
"""
from __future__ import annotations

import logging
import os
import secrets
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx
from eth_account import Account
from eth_utils import keccak
from web3 import Web3

# fcntl is POSIX-only — the cross-process wallet lock is a no-op on Windows.
try:
    import fcntl  # type: ignore
    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False

log = logging.getLogger("ardi.sdk")


class CoordinatorUnreachableError(RuntimeError):
    """Raised when the Coordinator HTTP endpoint can't be reached.

    The default Coordinator URL points at a developer ngrok tunnel which
    rotates regularly. When it's down, the underlying httpx error is
    cryptic; this wrapper makes the recovery path obvious.
    """


# ============================================================================
# Minimal ABIs — only the calls the SDK actually makes
# ============================================================================

# MockRandomness exposes a permissionless `fulfill(requestId)` that triggers
# the VRF callback synchronously. On real Chainlink VRF this is automatic;
# on testnet someone has to call it. The agent calls it itself after
# request_draw, treating it as part of the same logical step.
MOCK_RANDOMNESS_ABI = [
    {"type": "function", "name": "fulfill", "stateMutability": "nonpayable",
     "inputs": [{"type": "uint256", "name": "requestId"}], "outputs": []},
]

# Used to find requestId from the receipt of request_draw, so we can call fulfill.
DRAW_REQUESTED_EVENT_SIG = "DrawRequested(uint256,uint256,uint256,uint256)"

EPOCH_DRAW_ABI = [
    {"type": "event", "name": "DrawRequested", "anonymous": False,
     "inputs": [
         {"type": "uint256", "name": "epochId", "indexed": True},
         {"type": "uint256", "name": "wordId",  "indexed": True},
         {"type": "uint256", "name": "requestId"},
         {"type": "uint256", "name": "candidates"},
     ]},
    {"type": "function", "name": "openEpoch", "stateMutability": "nonpayable",
     "inputs": [{"type": "uint256", "name": "epochId"},
                {"type": "uint64", "name": "commitWindow"},
                {"type": "uint64", "name": "revealWindow"}],
     "outputs": []},
    {"type": "function", "name": "commit", "stateMutability": "payable",
     "inputs": [{"type": "uint256", "name": "epochId"},
                {"type": "uint256", "name": "wordId"},
                {"type": "bytes32", "name": "hash"}],
     "outputs": []},
    {"type": "function", "name": "reveal", "stateMutability": "nonpayable",
     "inputs": [{"type": "uint256", "name": "epochId"},
                {"type": "uint256", "name": "wordId"},
                {"type": "string", "name": "guess"},
                {"type": "bytes32", "name": "nonce"}],
     "outputs": []},
    {"type": "function", "name": "requestDraw", "stateMutability": "nonpayable",
     "inputs": [{"type": "uint256", "name": "epochId"},
                {"type": "uint256", "name": "wordId"}],
     "outputs": []},
    {"type": "function", "name": "winners", "stateMutability": "view",
     "inputs": [{"type": "uint256"}, {"type": "uint256"}],
     "outputs": [{"type": "address"}]},
    {"type": "function", "name": "epochs", "stateMutability": "view",
     "inputs": [{"type": "uint256"}],
     "outputs": [{"type": "uint64", "name": "startTs"},
                 {"type": "uint64", "name": "commitDeadline"},
                 {"type": "uint64", "name": "revealDeadline"},
                 {"type": "bool", "name": "exists"}]},
    {"type": "function", "name": "COMMIT_BOND", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "correctCount", "stateMutability": "view",
     "inputs": [{"type": "uint256"}, {"type": "uint256"}],
     "outputs": [{"type": "uint256"}]},
    # v1.0: getAnswer returns wordHash (not plaintext word). The plaintext is
    # supplied by the winner at inscribe time and verified against this hash.
    # For unsolved wordIds, plaintext never goes on chain.
    {"type": "function", "name": "getAnswer", "stateMutability": "view",
     "inputs": [{"type": "uint256", "name": "epochId"},
                {"type": "uint256", "name": "wordId"}],
     "outputs": [{"type": "bytes32", "name": "wordHash"},
                 {"type": "uint16",  "name": "power"},
                 {"type": "uint8",   "name": "languageId"},
                 {"type": "bool",    "name": "published"}]},
    {"type": "function", "name": "agentWinCount", "stateMutability": "view",
     "inputs": [{"type": "address"}], "outputs": [{"type": "uint8"}]},
    {"type": "function", "name": "MAX_WINS_PER_AGENT", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "uint8"}]},
    {"type": "function", "name": "wordCompromised", "stateMutability": "view",
     "inputs": [{"type": "uint256"}], "outputs": [{"type": "bool"}]},
    {"type": "event", "name": "Revealed", "anonymous": False,
     "inputs": [
         {"type": "uint256", "name": "epochId",  "indexed": True},
         {"type": "uint256", "name": "wordId",   "indexed": True},
         {"type": "address", "name": "agent",    "indexed": True},
         {"type": "bool",    "name": "isCorrect"},
     ]},
    # forfeitBond — sweep a stale commit's bond after the reveal window has
    # closed. Two-branch destination, decided by `answers[epoch][wordId].published`:
    #   - published = true  → bond → treasury  (agent committed but failed to reveal)
    #   - published = false → bond → agent     (Coordinator failed; no penalty)
    # Anyone can call (the destination is state-determined, not caller-determined).
    {"type": "function", "name": "forfeitBond", "stateMutability": "nonpayable",
     "inputs": [{"type": "uint256", "name": "epochId"},
                {"type": "uint256", "name": "wordId"},
                {"type": "address", "name": "agent"}],
     "outputs": []},
    {"type": "event", "name": "BondForfeited", "anonymous": False,
     "inputs": [
         {"type": "uint256", "name": "epochId", "indexed": True},
         {"type": "uint256", "name": "wordId",  "indexed": True},
         {"type": "address", "name": "agent",   "indexed": True},
         {"type": "uint256", "name": "amount"},
     ]},
    {"type": "event", "name": "BondRefundedNoAnswer", "anonymous": False,
     "inputs": [
         {"type": "uint256", "name": "epochId", "indexed": True},
         {"type": "uint256", "name": "wordId",  "indexed": True},
         {"type": "address", "name": "agent",   "indexed": True},
         {"type": "uint256", "name": "amount"},
     ]},
]

ARDI_NFT_ABI = [
    # v1.0: inscribe takes plaintext word + verifies on-chain hash match.
    # The contract reads (wordHash, power, lang) from EpochDraw and rejects
    # any word whose keccak doesn't equal wordHash.
    {"type": "function", "name": "inscribe", "stateMutability": "nonpayable",
     "inputs": [{"type": "uint64",  "name": "epochId"},
                {"type": "uint256", "name": "wordId"},
                {"type": "string",  "name": "word"}],
     "outputs": []},
    {"type": "function", "name": "fuse", "stateMutability": "nonpayable",
     "inputs": [{"type": "uint256", "name": "tokenIdA"},
                {"type": "uint256", "name": "tokenIdB"},
                {"type": "string", "name": "newWord"},
                {"type": "uint16", "name": "newPower"},
                {"type": "uint8", "name": "newLangId"},
                {"type": "bool", "name": "success"},
                {"type": "bytes", "name": "signature"}],
     "outputs": []},
    {"type": "function", "name": "ownerOf", "stateMutability": "view",
     "inputs": [{"type": "uint256"}],
     "outputs": [{"type": "address"}]},
    {"type": "function", "name": "agentMintCount", "stateMutability": "view",
     "inputs": [{"type": "address"}],
     "outputs": [{"type": "uint8"}]},
    # ERC-721 approval — needed before ArdiOTC.list. setApprovalForAll
    # is one-time (operator stays approved across listings).
    {"type": "function", "name": "setApprovalForAll", "stateMutability": "nonpayable",
     "inputs": [{"type": "address", "name": "operator"},
                {"type": "bool",    "name": "approved"}],
     "outputs": []},
    {"type": "function", "name": "isApprovedForAll", "stateMutability": "view",
     "inputs": [{"type": "address", "name": "owner"},
                {"type": "address", "name": "operator"}],
     "outputs": [{"type": "bool"}]},
]

BOND_ESCROW_ABI = [
    {"type": "function", "name": "registerMiner", "stateMutability": "nonpayable",
     "inputs": [], "outputs": []},
    {"type": "function", "name": "unlockBond", "stateMutability": "nonpayable",
     "inputs": [], "outputs": []},
    {"type": "function", "name": "isMiner", "stateMutability": "view",
     "inputs": [{"type": "address"}], "outputs": [{"type": "bool"}]},
    {"type": "function", "name": "BOND_AMOUNT", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "uint256"}]},
]

ERC20_ABI = [
    {"type": "function", "name": "approve", "stateMutability": "nonpayable",
     "inputs": [{"type": "address"}, {"type": "uint256"}],
     "outputs": [{"type": "bool"}]},
    {"type": "function", "name": "balanceOf", "stateMutability": "view",
     "inputs": [{"type": "address"}], "outputs": [{"type": "uint256"}]},
]

OTC_ABI = [
    # ArdiOTC marketplace — non-custodial fixed-price listings.
    # Sellers approve the OTC contract once (via NFT.setApprovalForAll),
    # then call list(tokenId, priceWei). Buyers send ETH equal to priceWei.
    # 100% to seller, no fee. Listings auto-expire if seller transfers.
    {"type": "function", "name": "list", "stateMutability": "nonpayable",
     "inputs": [{"type": "uint256", "name": "tokenId"},
                {"type": "uint256", "name": "priceWei"}],
     "outputs": []},
    {"type": "function", "name": "unlist", "stateMutability": "nonpayable",
     "inputs": [{"type": "uint256", "name": "tokenId"}],
     "outputs": []},
    {"type": "function", "name": "buy", "stateMutability": "payable",
     "inputs": [{"type": "uint256", "name": "tokenId"}],
     "outputs": []},
    {"type": "function", "name": "isListed", "stateMutability": "view",
     "inputs": [{"type": "uint256"}], "outputs": [{"type": "bool"}]},
    {"type": "function", "name": "getListing", "stateMutability": "view",
     "inputs": [{"type": "uint256"}],
     "outputs": [{"type": "tuple", "components": [
         {"type": "address", "name": "seller"},
         {"type": "uint256", "name": "priceWei"},
         {"type": "uint64",  "name": "listedAt"},
     ]}]},
    {"type": "event", "name": "Listed", "anonymous": False,
     "inputs": [
         {"type": "address", "name": "seller", "indexed": True},
         {"type": "uint256", "name": "tokenId", "indexed": True},
         {"type": "uint256", "name": "priceWei"},
     ]},
]

MINT_CTRL_ABI = [
    {"type": "function", "name": "claim", "stateMutability": "nonpayable",
     "inputs": [{"type": "uint256", "name": "day"},
                {"type": "uint256", "name": "amount"},
                {"type": "bytes32[]", "name": "proof"}],
     "outputs": []},
    {"type": "function", "name": "claimed", "stateMutability": "view",
     "inputs": [{"type": "uint256"}, {"type": "address"}],
     "outputs": [{"type": "bool"}]},
]


# ============================================================================
# Data types
# ============================================================================

@dataclass
class Riddle:
    word_id: int
    riddle: str
    power: int
    rarity: str
    language: str
    language_id: int
    hint_level: int


@dataclass
class CurrentEpoch:
    epoch_id: int
    start_ts: int
    commit_deadline: int
    reveal_deadline: int
    chain_id: int
    epoch_draw_contract: str
    ardi_nft_contract: str
    riddles: list[Riddle]


@dataclass
class CommitTicket:
    """Returned from `commit()` — KEEP THIS, you need it to reveal."""
    epoch_id: int
    word_id: int
    guess: str
    nonce: bytes
    tx_hash: str


# ============================================================================
# Client
# ============================================================================

class ArdiClient:
    """Single-agent client. Holds one private key and a web3 + http handle."""

    def __init__(
        self,
        rpc_url: str,
        coordinator_url: str,
        agent_private_key: str,
        contracts: dict[str, str],
        chain_id: int | None = None,
        gas_buffer: float = 1.2,
        request_timeout: int = 30,
    ):
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        self._account = Account.from_key(agent_private_key)
        self.coordinator_url = coordinator_url.rstrip("/")
        self._http = httpx.Client(timeout=request_timeout)
        self.chain_id = chain_id or self.w3.eth.chain_id
        self.gas_buffer = gas_buffer

        self._contracts = {k: Web3.to_checksum_address(v) for k, v in contracts.items()}
        self._draw = self.w3.eth.contract(
            address=self._contracts["epoch_draw"], abi=EPOCH_DRAW_ABI)
        self._nft = self.w3.eth.contract(
            address=self._contracts["ardi_nft"], abi=ARDI_NFT_ABI)
        self._escrow = self.w3.eth.contract(
            address=self._contracts["bond_escrow"], abi=BOND_ESCROW_ABI)
        self._mint_ctrl = self.w3.eth.contract(
            address=self._contracts["mint_controller"], abi=MINT_CTRL_ABI)
        # ArdiOTC marketplace — optional. Caller may pass `ardi_otc` in the
        # contracts dict; if absent, market commands raise a clear error.
        otc_addr = self._contracts.get("ardi_otc", "")
        if otc_addr and int(otc_addr, 16) != 0:
            self._otc = self.w3.eth.contract(
                address=otc_addr, abi=OTC_ABI)
        else:
            self._otc = None
        # MockRandomness — only present on testnet rehearsals where the
        # operator deploys a mock VRF. Real Chainlink VRF doesn't expose
        # public fulfill(); on mainnet this address won't be in `contracts`.
        mock_rng_addr = self._contracts.get("mock_randomness", "")
        if mock_rng_addr and int(mock_rng_addr, 16) != 0:
            self._mock_rng = self.w3.eth.contract(
                address=mock_rng_addr, abi=MOCK_RANDOMNESS_ABI)
        else:
            self._mock_rng = None

    @property
    def address(self) -> str:
        return self._account.address

    @property
    def contracts(self) -> dict[str, str]:
        """Public read-only view of the resolved checksum addresses."""
        return dict(self._contracts)

    # ------------------------------------------------ cross-process lock --

    @contextmanager
    def _wallet_lock(self):
        """Per-wallet POSIX flock. Serializes _send across processes that
        share the same private key — eliminates the cross-process nonce race
        that the in-process monotonic cache can't see (e.g. running both
        `ardi-agent mine` and `ardi-agent play` against the same wallet).

        On Windows / no-fcntl environments this is a no-op; the in-process
        nonce cache is still in effect, so it's only the multi-process case
        that degrades.
        """
        if not _HAS_FCNTL:
            yield
            return
        lock_dir = Path(
            os.environ.get("ARDI_HOME", str(Path.home() / ".ardi"))
        ) / "locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = lock_dir / f"{self._account.address.lower()}.lock"
        f = open(lock_path, "w")
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            f.close()

    # ------------------------------------------------------ log scan helper --

    def _get_logs_chunked(
        self,
        address: str,
        topics: list,
        lookback_blocks: int = 6000,
        chunk_size: int = 1000,
    ) -> list:
        """get_logs but resilient to public-RPC range limits.

        Many RPCs cap eth_getLogs to 1024–2048 blocks per call; querying
        5000 in one shot returns 400/-32600. We walk backwards in chunks,
        and on errors halve the chunk size and retry once before moving on.
        Returns logs in chronological order (oldest first) so callers can
        `max(..., key=blockNumber)` deterministically.
        """
        latest = self.w3.eth.block_number
        end = latest
        floor = max(0, latest - lookback_blocks)
        all_logs: list = []
        cur_chunk = chunk_size
        while end >= floor:
            from_b = max(floor, end - cur_chunk + 1)
            try:
                logs = self.w3.eth.get_logs({
                    "address": address,
                    "topics": topics,
                    "fromBlock": from_b,
                    "toBlock": end,
                })
                all_logs.extend(logs)
                end = from_b - 1
            except Exception as e:
                # Halve chunk size and retry the same range; if already at
                # the floor, give up on this shard and move on.
                if cur_chunk > 100:
                    cur_chunk = max(100, cur_chunk // 2)
                    log.debug(f"get_logs range too large, retrying with chunk={cur_chunk}: {e}")
                    continue
                log.warning(f"get_logs failed at [{from_b},{end}], skipping: {e}")
                end = from_b - 1
        all_logs.sort(key=lambda L: (L["blockNumber"], L.get("logIndex", 0)))
        return all_logs

    # --------------------------------------------------------- coord HTTP --

    def _coord_request(self, method: str, path: str, **kw):
        """Wrapper around self._http.{get,post} that converts cryptic
        connection errors into a CoordinatorUnreachableError with the
        recovery hint baked in. Other HTTP errors pass through."""
        url = f"{self.coordinator_url}{path}"
        try:
            r = self._http.request(method, url, **kw)
        except (httpx.ConnectError, httpx.ConnectTimeout,
                httpx.ReadTimeout, httpx.ReadError) as e:
            raise CoordinatorUnreachableError(
                f"Coordinator not reachable at {self.coordinator_url}\n"
                f"  → original: {type(e).__name__}: {e}\n\n"
                f"The default URL points at a developer ngrok tunnel which "
                f"rotates frequently. Ask the operator for the current URL "
                f"and re-run with:\n"
                f"    export ARDI_COORDINATOR_URL=https://<new-tunnel>\n"
            ) from e
        return r

    # -------------------------------------------------------------------- tx --

    # Nonce-related transient errors that we retry by re-reading the chain's
    # pending nonce. These come back as web3.exceptions.Web3RPCError which
    # we string-match by message because the error structure differs between
    # public RPCs.
    _NONCE_TRANSIENT = (
        "nonce too low",
        "replacement transaction underpriced",
        "already known",
        "OldNonce",
    )

    def _send(
        self,
        contract_call,
        value: int = 0,
        gas: int | None = None,
        max_attempts: int = 4,
    ) -> str:
        """Build, sign, send, wait. Returns tx hash hex.

        Nonce strategy — back-to-back sends on public RPCs are fragile because
        the RPC's view of "pending" can be stale for 1-3 seconds after a
        previous send. Mitigations:

          1. Use the 'pending' nonce tag.
          2. Layer a local monotonic cache on top so we never reuse a slot
             we've already burned in this process.
          3. **POSIX flock** (`_wallet_lock`) so other processes sharing
             the same private key block here instead of racing nonces.
          4. Retry on `nonce too low` / `replacement transaction underpriced` /
             `already known` — bust the cache, sleep briefly, re-read.

        On success, advance the cache exactly once (after `send_raw_transaction`
        is accepted). On failure, the cache stays where it was — callers can
        invoke us again and we'll fall back to whatever the chain reports.
        """
        import time as _time
        addr = self._account.address
        last_err: Exception | None = None

        # Hold the wallet flock for the entire build → sign → send → receipt
        # cycle. This serializes _send across processes that share the same
        # private key, eliminating cross-process nonce races.
        with self._wallet_lock():
            for attempt in range(max_attempts):
                chain_nonce = self.w3.eth.get_transaction_count(addr, "pending")
                cached = getattr(self, "_last_nonce", -1)
                # First attempt: trust the cache. Retries: trust the chain.
                nonce = max(chain_nonce, cached + 1) if attempt == 0 else chain_nonce

                tx = contract_call.build_transaction({
                    "from": addr,
                    "nonce": nonce,
                    "chainId": self.chain_id,
                    "gasPrice": self.w3.eth.gas_price,
                    "value": value,
                    "gas": gas or self._estimate_gas(contract_call, value),
                })
                signed = self._account.sign_transaction(tx)

                try:
                    h = self.w3.eth.send_raw_transaction(signed.raw_transaction)
                except Exception as e:
                    last_err = e
                    msg = str(e).lower()
                    if any(s.lower() in msg for s in self._NONCE_TRANSIENT):
                        log.warning(
                            "nonce-transient on attempt %d (nonce=%d, chain=%d): %s — retrying",
                            attempt + 1, nonce, chain_nonce, msg[:120],
                        )
                        self._last_nonce = -1            # bust cache
                        _time.sleep(2.0 + attempt)        # backoff
                        continue
                    raise

                # Accepted by mempool — commit cursor + wait for receipt
                self._last_nonce = nonce
                receipt = self.w3.eth.wait_for_transaction_receipt(h, timeout=180)
                if receipt.status != 1:
                    raise RuntimeError(f"tx reverted: {h.hex()}")
                return h.hex()

            raise RuntimeError(
                f"send failed after {max_attempts} attempts; last error: {last_err}"
            )

    def _estimate_gas(self, contract_call, value: int) -> int:
        try:
            est = contract_call.estimate_gas({"from": self._account.address, "value": value})
            return int(est * self.gas_buffer)
        except Exception:
            # Conservative fallback if estimator can't simulate (revert paths etc.)
            return 500_000

    # ------------------------------------------------------ Coordinator HTTP --

    def fetch_current_epoch(self) -> CurrentEpoch:
        r = self._coord_request("GET", "/v1/epoch/current")
        r.raise_for_status()
        d = r.json()
        return CurrentEpoch(
            epoch_id=d["epochId"],
            start_ts=d["startTs"],
            commit_deadline=d["commitDeadline"],
            reveal_deadline=d["revealDeadline"],
            chain_id=d["chainId"],
            epoch_draw_contract=d["epochDrawContract"],
            ardi_nft_contract=d["ardiNftContract"],
            riddles=[
                Riddle(
                    word_id=r["wordId"], riddle=r["riddle"], power=r["power"],
                    rarity=r["rarity"], language=r["language"],
                    language_id=r["languageId"], hint_level=r["hintLevel"],
                )
                for r in d["riddles"]
            ],
        )

    def fetch_airdrop_proof(self, day: int) -> dict | None:
        r = self._coord_request("GET", f"/v1/airdrop/proof/{day}/{self.address}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    # -------------------------------------------------------- Commit-reveal --

    def commit_hash(self, guess: str, nonce: bytes) -> bytes:
        """Build the exact hash the contract expects:
            keccak256(abi.encodePacked(guess, msg.sender, nonce))
        """
        if not isinstance(nonce, (bytes, bytearray)) or len(nonce) != 32:
            raise ValueError("nonce must be 32 bytes")
        return keccak(
            guess.encode("utf-8")
            + bytes.fromhex(self.address.lower().removeprefix("0x"))
            + bytes(nonce)
        )

    def commit(
        self,
        epoch_id: int,
        word_id: int,
        guess: str,
        nonce: bytes | None = None,
    ) -> CommitTicket:
        """Submit a sealed commit. Returns a CommitTicket the agent MUST persist
        until reveal (loses bond if reveal data is forgotten)."""
        if nonce is None:
            nonce = secrets.token_bytes(32)
        h = self.commit_hash(guess, nonce)
        bond = int(self._draw.functions.COMMIT_BOND().call())
        tx_hash = self._send(
            self._draw.functions.commit(epoch_id, word_id, h),
            value=bond,
            gas=120_000,
        )
        log.info(f"commit ok: epoch={epoch_id} word={word_id} bond={bond} tx={tx_hash}")
        return CommitTicket(
            epoch_id=epoch_id, word_id=word_id, guess=guess,
            nonce=bytes(nonce), tx_hash=tx_hash,
        )

    def is_answer_published(self, epoch_id: int, word_id: int) -> bool:
        """View call — has Coordinator already publishAnswer'd for this slot?

        Note: getAnswer() never reverts (returns the zero-struct for unknown
        slots, with published=False). So any exception here is RPC/network,
        not contract logic — we propagate it instead of swallowing, otherwise
        a flaky RPC would silently look identical to "not published yet".
        """
        result = self._draw.functions.getAnswer(epoch_id, word_id).call()
        # Returns (word, power, languageId, published)
        return bool(result[3])

    def wait_for_answer_published(
        self,
        epoch_id: int,
        word_id: int,
        timeout: int = 90,
        poll_interval: float = 4.0,
        max_consecutive_rpc_errors: int = 5,
    ) -> bool:
        """Block until the Coordinator's publishAnswer tx is mined for this
        (epoch, wordId), or timeout. Without this, calling reveal() too early
        reverts with AnswerNotPublished, costing the agent gas + a nail-biting
        diagnostic. Returns True if published, False on timeout.

        Tolerates transient RPC errors (logs + retries). After
        ``max_consecutive_rpc_errors`` failures in a row we raise — at that
        point the RPC is unhealthy and silently looping would only deepen
        the confusion.
        """
        import time as _time
        deadline = _time.time() + timeout
        consecutive_errors = 0
        last_err: Exception | None = None
        while _time.time() < deadline:
            try:
                if self.is_answer_published(epoch_id, word_id):
                    return True
                consecutive_errors = 0
            except Exception as e:
                consecutive_errors += 1
                last_err = e
                log.warning(
                    f"is_answer_published RPC error #{consecutive_errors}: {e}"
                )
                if consecutive_errors >= max_consecutive_rpc_errors:
                    raise RuntimeError(
                        f"RPC unhealthy: {consecutive_errors} consecutive "
                        f"errors checking getAnswer({epoch_id},{word_id}). "
                        f"Last: {last_err}"
                    ) from last_err
            _time.sleep(poll_interval)
        return False

    # Sentinel returned by reveal() instead of raising — so callers can
    # distinguish "Coordinator hasn't published yet" from "your commit hash
    # doesn't match" (real bug) without scraping revert messages.
    REVEAL_NOT_PUBLISHED = "ANSWER_NOT_PUBLISHED"

    def reveal(
        self,
        epoch_id: int,
        word_id: int,
        guess: str,
        nonce: bytes,
        *,
        wait_for_publish: bool = True,
        wait_timeout: int = 90,
    ) -> dict:
        """Reveal a previously-committed guess.

        Returns a dict:
          { ok: True,  tx_hash: str, correct: bool }   — reveal landed
          { ok: False, status: 'ANSWER_NOT_PUBLISHED' } — Coordinator hasn't
            published yet (don't burn gas, retry later)

        If `wait_for_publish=True` (default), the SDK polls the on-chain
        getAnswer(...).published flag for `wait_timeout` seconds before
        sending the tx. This avoids the most common failure mode: calling
        reveal too soon after commit window closes, before Coordinator's
        publishAnswer tx is mined.
        """
        if wait_for_publish:
            published = self.wait_for_answer_published(
                epoch_id, word_id, timeout=wait_timeout,
            )
            if not published:
                log.warning(
                    f"reveal: epoch={epoch_id} word={word_id} — Coordinator "
                    f"never published in {wait_timeout}s; not sending tx"
                )
                return {"ok": False, "status": self.REVEAL_NOT_PUBLISHED}

        # Real send. If it still reverts at this point, it's a genuine
        # commit-mismatch (wrong nonce / wrong guess relative to commit hash).
        tx_hash = self._send(
            self._draw.functions.reveal(epoch_id, word_id, guess, nonce),
            gas=180_000,
        )
        log.info(f"reveal ok: epoch={epoch_id} word={word_id} tx={tx_hash}")

        # Parse the Revealed event log to learn whether the guess was
        # CORRECT (entered candidate pool) or just well-formed (commit hash
        # matched but guess != canonical answer). Big UX delta — agent now
        # knows which words to spend gas on for inscribe.
        is_correct = self._parse_revealed_event(tx_hash, epoch_id, word_id)
        return {
            "ok": True,
            "tx_hash": tx_hash,
            "correct": is_correct,
        }

    def _parse_revealed_event(
        self,
        tx_hash: str,
        epoch_id: int,
        word_id: int,
    ) -> bool | None:
        """Pull the Revealed(epochId, wordId, agent, isCorrect) event out
        of the receipt and return its `isCorrect` flag. Returns None if the
        event isn't found (shouldn't happen, but be defensive)."""
        try:
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            sig = self.w3.keccak(text="Revealed(uint256,uint256,address,bool)")
            for L in receipt.logs:
                if (len(L["topics"]) >= 4
                        and L["topics"][0] == sig
                        and int(L["topics"][1].hex(), 16) == int(epoch_id)
                        and int(L["topics"][2].hex(), 16) == int(word_id)):
                    # data is the un-indexed bool: 32 bytes, last byte is 0/1
                    data = L["data"]
                    if hasattr(data, "hex"):
                        data = data.hex()
                    return data.endswith("1")
        except Exception as e:
            log.warning(f"couldn't parse Revealed event: {e}")
        return None

    def request_draw(self, epoch_id: int, word_id: int) -> str:
        """Trigger VRF for this slot. Anyone can call — costs the caller gas."""
        tx_hash = self._send(
            self._draw.functions.requestDraw(epoch_id, word_id),
            gas=200_000,
        )
        return tx_hash

    def fulfill_pending_for(self, epoch_id: int, word_id: int) -> str | None:
        """Look up the most recent DrawRequested event for (epoch, wordId) and
        call MockRandomness.fulfill(requestId) so the VRF callback fires.

        Real Chainlink VRF auto-fulfills asynchronously — this is testnet only.
        Returns the fulfill tx hash, or None if no MockRandomness deployed
        (mainnet) / no DrawRequested log found / already fulfilled.

        Uses the chunked log scanner to stay under public-RPC range limits
        (many cap eth_getLogs at 1024 blocks; we walk back in 1000-block
        shards up to ~6K blocks of history).
        """
        if self._mock_rng is None:
            return None
        event_sig = self.w3.keccak(text=DRAW_REQUESTED_EVENT_SIG).hex()
        if not event_sig.startswith("0x"):
            event_sig = "0x" + event_sig
        epoch_topic = "0x" + int(epoch_id).to_bytes(32, "big").hex()
        word_topic  = "0x" + int(word_id).to_bytes(32, "big").hex()
        logs = self._get_logs_chunked(
            address=self._draw.address,
            topics=[event_sig, epoch_topic, word_topic],
            lookback_blocks=6000,
        )
        if not logs:
            log.warning(f"fulfill: no DrawRequested log for ({epoch_id}, {word_id})")
            return None
        latest_log = max(logs, key=lambda L: L["blockNumber"])
        # data: requestId (uint256, first 32 bytes) + candidates (uint256)
        data = latest_log["data"]
        if hasattr(data, "hex"):
            data = data.hex()
        if data.startswith("0x"):
            data = data[2:]
        request_id = int(data[:64], 16)
        try:
            tx_hash = self._send(
                self._mock_rng.functions.fulfill(request_id),
                gas=300_000,
            )
            log.info(f"fulfill ok: request_id={request_id} tx={tx_hash}")
            return tx_hash
        except Exception as e:
            # Common case: already fulfilled (UnknownRequest revert) — that's fine
            log.info(f"fulfill: request_id={request_id} skipped: {e}")
            return None

    def word_ids_for_epoch(
        self,
        epoch_id: int,
        lookback_blocks: int = 20000,
    ) -> list[int]:
        """Discover wordIds that had a draw requested in the given epoch.

        Used by `winners` when the caller doesn't pass --word-id and the
        epoch isn't current (so we can't get the riddles list from the
        Coordinator). Naively assuming wordIds are 0..14 is wrong — they
        are global vault indices 0..20999 chosen per-epoch. The on-chain
        `DrawRequested` event is the source of truth for "which wordIds
        had at least one correct revealer in this epoch", which is exactly
        the set that can have a winner.
        """
        event_sig = self.w3.keccak(text=DRAW_REQUESTED_EVENT_SIG).hex()
        if not event_sig.startswith("0x"):
            event_sig = "0x" + event_sig
        epoch_topic = "0x" + int(epoch_id).to_bytes(32, "big").hex()
        logs = self._get_logs_chunked(
            address=self._draw.address,
            topics=[event_sig, epoch_topic],
            lookback_blocks=lookback_blocks,
        )
        word_ids = sorted({int(L["topics"][2].hex(), 16) for L in logs})
        return word_ids

    def winner_of(self, epoch_id: int, word_id: int) -> str:
        return self._draw.functions.winners(epoch_id, word_id).call()

    def correct_count(self, epoch_id: int, word_id: int) -> int:
        return int(self._draw.functions.correctCount(epoch_id, word_id).call())

    def epoch_state(self, epoch_id: int) -> dict:
        startTs, commitDl, revealDl, exists = self._draw.functions.epochs(epoch_id).call()
        return {
            "exists": exists, "start_ts": startTs,
            "commit_deadline": commitDl, "reveal_deadline": revealDl,
            "now": int(time.time()),
            "phase": (
                "not-open" if not exists else
                "commit"   if int(time.time()) < commitDl else
                "reveal"   if int(time.time()) < revealDl else
                "draw"
            ),
        }

    # -------------------------------------------------------- Bond + KYA --

    def is_miner(self) -> bool:
        return bool(self._escrow.functions.isMiner(self.address).call())

    def register_miner(self, awp_token: str | None = None) -> str:
        """Approve 10K $AWP + register. Caller MUST already be KYA-verified."""
        bond_amount = int(self._escrow.functions.BOND_AMOUNT().call())
        awp_addr = awp_token or self._contracts.get("awp_token") or self._contracts.get("mock_awp")
        if not awp_addr:
            raise ValueError("awp_token address not provided")
        awp = self.w3.eth.contract(
            address=Web3.to_checksum_address(awp_addr), abi=ERC20_ABI)
        bal = int(awp.functions.balanceOf(self.address).call())
        if bal < bond_amount:
            raise RuntimeError(f"AWP balance {bal} < required {bond_amount}")
        # 1. Approve
        self._send(awp.functions.approve(self._contracts["bond_escrow"], bond_amount), gas=80_000)
        # 2. Register
        return self._send(self._escrow.functions.registerMiner(), gas=200_000)

    def unlock_bond(self) -> str:
        return self._send(self._escrow.functions.unlockBond(), gas=120_000)

    # ----------------------------------------------------------- Inscribe --

    def inscribe(self, epoch_id: int, word_id: int, word: str) -> str:
        """Mint the Ardinal. v1.0: caller must supply the plaintext `word` —
        the contract verifies `keccak256(word) == answer.wordHash` (the
        published hash). Caller must be the winner_of(epoch, word).

        Typical flow: agent committed `guess`; if their reveal had
        `correct=True` and they won the VRF, they pass that same `guess`
        in here. The TicketStore keeps it across restarts so this is
        always recoverable."""
        return self._send(
            self._nft.functions.inscribe(epoch_id, word_id, word),
            gas=300_000,
        )

    def mint_count(self) -> int:
        """How many NFTs this agent has actually inscribed.
        For "wins consumed" use win_count() — that's the cap-binding number."""
        return int(self._nft.functions.agentMintCount(self.address).call())

    def win_count(self) -> int:
        """How many lottery wins this agent has accumulated. Increments at
        VRF callback (whether the win is later inscribed or not). The
        on-chain cap is enforced against THIS counter, not mint_count."""
        return int(self._draw.functions.agentWinCount(self.address).call())

    def max_wins(self) -> int:
        """Per-agent win cap, read from EpochDraw.MAX_WINS_PER_AGENT."""
        return int(self._draw.functions.MAX_WINS_PER_AGENT().call())

    def is_word_compromised(self, word_id: int) -> bool:
        """True iff this wordId has had at least one correct on-chain reveal —
        in which case its plaintext is leaked via tx calldata and the
        Coordinator must permanently exclude it from selection."""
        return bool(self._draw.functions.wordCompromised(word_id).call())

    # ----------------------------------------------------------- Bond recovery --

    def forfeit_bond(
        self,
        epoch_id: int,
        word_id: int,
        agent: str | None = None,
    ) -> dict:
        """Sweep a stale commit's bond after the reveal window has closed.

        The on-chain branch is decided by `answers[epoch][wordId].published`:
          - published=True  → bond goes to treasury (agent committed but never
            revealed; this is the anti-grief path).
          - published=False → bond is **refunded to the original committer**
            (Coordinator failed; no penalty for the agent).

        Anyone can call this — the destination is state-determined. Most
        often the agent calls it themselves to recover a stuck bond when
        the Coordinator was offline / never published the canonical answer.

        Returns:
            { ok: True, tx_hash, refunded: bool, amount_wei: int }
              refunded=True  → bond came back to `agent`
              refunded=False → bond was forfeited to treasury

        Raises if the call reverts (reveal window not closed yet, no commit
        exists, already revealed, already claimed, etc.). Caller should
        handle by checking `epoch_state(epoch_id)["phase"] == "draw"` first.
        """
        target = Web3.to_checksum_address(agent) if agent else self.address
        tx_hash = self._send(
            self._draw.functions.forfeitBond(epoch_id, word_id, target),
            gas=120_000,
        )
        refunded, amount = self._parse_forfeit_result(tx_hash, epoch_id, word_id, target)
        log.info(
            f"forfeit_bond ok: epoch={epoch_id} word={word_id} agent={target} "
            f"refunded={refunded} amount={amount} tx={tx_hash}"
        )
        return {
            "ok": True,
            "tx_hash": tx_hash,
            "refunded": refunded,
            "amount_wei": amount,
        }

    def _parse_forfeit_result(
        self,
        tx_hash: str,
        epoch_id: int,
        word_id: int,
        agent: str,
    ) -> tuple[bool, int]:
        """Return (refunded_to_agent, amount_wei) from the receipt's events.
        refunded=True means BondRefundedNoAnswer; False means BondForfeited
        (treasury). Filters by all three indexed topics — epoch, wordId, and
        agent — so a tx that happened to emit two events (different agents)
        wouldn't cross-match. Defaults to (False, 0) if neither event found."""
        try:
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            sig_refund = self.w3.keccak(text="BondRefundedNoAnswer(uint256,uint256,address,uint256)")
            sig_forfeit = self.w3.keccak(text="BondForfeited(uint256,uint256,address,uint256)")
            agent_lo = Web3.to_checksum_address(agent).lower()
            for L in receipt.logs:
                if len(L["topics"]) < 4:
                    continue
                if (int(L["topics"][1].hex(), 16) != int(epoch_id)
                        or int(L["topics"][2].hex(), 16) != int(word_id)):
                    continue
                # topics[3] is indexed address — last 20 bytes of 32-byte topic
                topic_agent = "0x" + L["topics"][3].hex()[-40:]
                if topic_agent.lower() != agent_lo:
                    continue
                # data is the un-indexed amount: 32 bytes uint256
                data = L["data"]
                if hasattr(data, "hex"):
                    data = data.hex()
                if data.startswith("0x"):
                    data = data[2:]
                amount = int(data[:64], 16) if data else 0
                if L["topics"][0] == sig_refund:
                    return True, amount
                if L["topics"][0] == sig_forfeit:
                    return False, amount
        except Exception as e:
            log.warning(f"couldn't parse forfeit events: {e}")
        return False, 0

    # ----------------------------------------------------------- Fusion --

    def forge_quote(self, token_a: int, token_b: int) -> dict:
        """Read-only LLM oracle preview from Coordinator. No tx."""
        r = self._coord_request(
            "POST", "/v1/forge/quote",
            json={"tokenIdA": token_a, "tokenIdB": token_b, "holder": self.address},
        )
        r.raise_for_status()
        return r.json()

    def forge_sign(self, token_a: int, token_b: int) -> dict:
        """Get a Coordinator-signed fuse authorization. Returns the kwargs
        you'd pass to `fuse()`."""
        r = self._coord_request(
            "POST", "/v1/forge/sign",
            json={"tokenIdA": token_a, "tokenIdB": token_b, "holder": self.address},
        )
        r.raise_for_status()
        return r.json()

    def fuse(self, sig_response: dict) -> str:
        """Submit the Coordinator-signed fuse authorization on-chain."""
        return self._send(
            self._nft.functions.fuse(
                sig_response["tokenIdA"],
                sig_response["tokenIdB"],
                sig_response["newWord"],
                int(sig_response["newPower"]),
                int(sig_response["newLanguageId"]),
                bool(sig_response["success"]),
                bytes.fromhex(sig_response["signature"].removeprefix("0x")),
            ),
            gas=400_000,
        )

    # ----------------------------------------------------------- Settlement --

    def claim_airdrop(self, day: int) -> str:
        """Pull dual-token Merkle proof from Coordinator and call
        ArdiMintController.claim(day, ardiAmount, awpAmount, proof).

        Coordinator endpoint shape (post-AWP-alignment):
          { ardiAmount: str(uint256), awpAmount: str(uint256), proof: [hex,...] }

        Falls back to legacy {amount, proof} shape if the operator hasn't
        upgraded the Coordinator yet — sets awpAmount=0 in that case.
        """
        proof_data = self.fetch_airdrop_proof(day)
        if not proof_data:
            raise RuntimeError(f"no airdrop entry for {self.address} on day {day}")
        # Dual-token (current) shape
        ardi_amount = int(proof_data.get("ardiAmount", proof_data.get("amount", 0)))
        awp_amount = int(proof_data.get("awpAmount", 0))
        proof = [bytes.fromhex(p.removeprefix("0x")) for p in proof_data["proof"]]
        return self._send(
            self._mint_ctrl.functions.claim(day, ardi_amount, awp_amount, proof),
            gas=400_000,
        )

    def already_claimed(self, day: int) -> bool:
        return bool(self._mint_ctrl.functions.claimed(day, self.address).call())

    # ============================================================ Market =====

    def _require_otc(self) -> None:
        """Marketplace methods need the OTC contract address wired into
        `contracts['ardi_otc']` at client construction. Raise a friendly
        error if it isn't there (e.g. caller built the client with an
        old deploy.json that pre-dates ArdiOTC)."""
        if self._otc is None:
            raise RuntimeError(
                "ArdiOTC contract not configured. Add `ardi_otc` to your "
                "contracts dict (it's `otc` in deployments/base-sepolia.json) "
                "or upgrade your deploy fetcher."
            )

    def market_listings(self, lookback_blocks: int = 20000) -> list[dict]:
        """Return all currently-active OTC listings.

        Strategy: scan Listed events for tokenIds ever offered, then
        filter to live ones via `isListed` + `getListing`. A listing
        is considered alive iff (a) `isListed=true` AND (b) the seller
        recorded in the listing still owns the token (otherwise the
        contract would refund any buy attempt).

        Returns a list of dicts: {token_id, seller, price_wei, price_eth,
        listed_at}.
        """
        self._require_otc()
        sig = self.w3.keccak(text="Listed(address,uint256,uint256)").hex()
        if not sig.startswith("0x"):
            sig = "0x" + sig
        logs = self._get_logs_chunked(
            address=self._otc.address,
            topics=[sig],
            lookback_blocks=lookback_blocks,
        )
        seen: set[int] = set()
        ordered_ids: list[int] = []
        for L in logs:
            tid = int(L["topics"][2].hex(), 16)
            if tid not in seen:
                seen.add(tid)
                ordered_ids.append(tid)
        out: list[dict] = []
        for tid in ordered_ids:
            try:
                if not bool(self._otc.functions.isListed(tid).call()):
                    continue
                lst = self._otc.functions.getListing(tid).call()
                # lst is (seller, priceWei, listedAt)
                seller, price_wei, listed_at = lst[0], int(lst[1]), int(lst[2])
                # Stale-seller filter: the contract will revert at buy if the
                # seller has transferred away, so we hide those entries.
                cur_owner = self._nft.functions.ownerOf(tid).call()
                if cur_owner.lower() != seller.lower():
                    continue
                out.append({
                    "token_id": tid,
                    "seller": seller,
                    "price_wei": price_wei,
                    "price_eth": price_wei / 1e18,
                    "listed_at": listed_at,
                })
            except Exception as e:
                log.debug(f"market_listings: skipping {tid}: {e}")
        return out

    def market_listing_of(self, token_id: int) -> dict | None:
        """Single-token getListing helper. Returns None if not listed."""
        self._require_otc()
        try:
            if not bool(self._otc.functions.isListed(token_id).call()):
                return None
            lst = self._otc.functions.getListing(token_id).call()
            return {
                "token_id": token_id,
                "seller": lst[0],
                "price_wei": int(lst[1]),
                "price_eth": int(lst[1]) / 1e18,
                "listed_at": int(lst[2]),
            }
        except Exception:
            return None

    def market_list(self, token_id: int, price_eth: float) -> dict:
        """List one of your Ardinals on ArdiOTC at fixed ETH price.

        Auto-handles the ERC-721 approval prerequisite: if the OTC contract
        isn't yet an approved operator for the caller, sends one
        `setApprovalForAll(otc, true)` tx first. Subsequent `market_list`
        calls reuse that approval.

        Returns: {ok, list_tx, approval_tx (or None), price_wei, price_eth}.
        """
        self._require_otc()
        if price_eth <= 0:
            raise ValueError("price must be > 0")
        # Ownership check — fail-fast vs. waiting for the contract revert
        owner = self._nft.functions.ownerOf(token_id).call()
        if owner.lower() != self.address.lower():
            raise RuntimeError(
                f"you don't own tokenId {token_id} (owner is {owner})"
            )
        # Approval check
        approved = bool(self._nft.functions.isApprovedForAll(
            self.address, self._otc.address
        ).call())
        approval_tx = None
        if not approved:
            approval_tx = self._send(
                self._nft.functions.setApprovalForAll(self._otc.address, True),
                gas=80_000,
            )
            log.info(f"market_list: approved OTC operator (tx={approval_tx})")
        price_wei = int(price_eth * 1e18)
        list_tx = self._send(
            self._otc.functions.list(token_id, price_wei),
            gas=120_000,
        )
        return {
            "ok": True,
            "list_tx": list_tx,
            "approval_tx": approval_tx,
            "price_wei": price_wei,
            "price_eth": price_eth,
        }

    def market_unlist(self, token_id: int) -> str:
        """Cancel your active listing for tokenId. Returns tx hash."""
        self._require_otc()
        return self._send(
            self._otc.functions.unlist(token_id),
            gas=80_000,
        )

    def market_buy(self, token_id: int, max_price_eth: float | None = None) -> dict:
        """Buy a listed Ardinal. Sends ETH equal to the on-chain priceWei
        as msg.value; the contract refunds any excess so over-paying is
        safe but unnecessary.

        Args:
          max_price_eth: optional ceiling. If the on-chain price exceeds
                         this, raise without sending — protects against
                         a seller bumping the price between quote and buy.

        Returns: {ok, tx_hash, price_wei, price_eth, seller}.
        """
        self._require_otc()
        listing = self.market_listing_of(token_id)
        if not listing:
            raise RuntimeError(f"tokenId {token_id} is not listed")
        price_wei = listing["price_wei"]
        if max_price_eth is not None and price_wei > int(max_price_eth * 1e18):
            raise RuntimeError(
                f"on-chain price {listing['price_eth']:.6f} ETH exceeds "
                f"--max-price {max_price_eth} ETH"
            )
        if listing["seller"].lower() == self.address.lower():
            raise RuntimeError("you can't buy your own listing — use market_unlist")
        tx = self._send(
            self._otc.functions.buy(token_id),
            value=price_wei,
            gas=200_000,
        )
        return {
            "ok": True,
            "tx_hash": tx,
            "price_wei": price_wei,
            "price_eth": listing["price_eth"],
            "seller": listing["seller"],
        }
