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
import secrets
import time
from dataclasses import dataclass
from typing import Optional

import httpx
from eth_account import Account
from eth_utils import keccak
from web3 import Web3

log = logging.getLogger("ardi.sdk")


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
]

ARDI_NFT_ABI = [
    {"type": "function", "name": "inscribe", "stateMutability": "nonpayable",
     "inputs": [{"type": "uint64", "name": "epochId"},
                {"type": "uint256", "name": "wordId"}],
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
          3. Retry on `nonce too low` / `replacement transaction underpriced` /
             `already known` — bust the cache, sleep briefly, re-read.

        On success, advance the cache exactly once (after `send_raw_transaction`
        is accepted). On failure, the cache stays where it was — callers can
        invoke us again and we'll fall back to whatever the chain reports.
        """
        import time as _time
        addr = self._account.address
        last_err: Exception | None = None

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
        r = self._http.get(f"{self.coordinator_url}/v1/epoch/current")
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
        url = f"{self.coordinator_url}/v1/airdrop/proof/{day}/{self.address}"
        r = self._http.get(url)
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

    def reveal(
        self,
        epoch_id: int,
        word_id: int,
        guess: str,
        nonce: bytes,
    ) -> str:
        """Reveal the previously-committed guess. Bond refunded on success."""
        tx_hash = self._send(
            self._draw.functions.reveal(epoch_id, word_id, guess, nonce),
            gas=180_000,
        )
        log.info(f"reveal ok: epoch={epoch_id} word={word_id} tx={tx_hash}")
        return tx_hash

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
        """
        if self._mock_rng is None:
            return None
        event_sig = self.w3.keccak(text=DRAW_REQUESTED_EVENT_SIG).hex()
        if not event_sig.startswith("0x"):
            event_sig = "0x" + event_sig
        epoch_topic = "0x" + int(epoch_id).to_bytes(32, "big").hex()
        word_topic  = "0x" + int(word_id).to_bytes(32, "big").hex()
        # Scan a window of recent blocks. Base Sepolia is 2s blocks; 5K
        # covers the last ~3h which is way more than the per-epoch cycle.
        latest = self.w3.eth.block_number
        from_block = max(0, latest - 5000)
        try:
            logs = self.w3.eth.get_logs({
                "address": self._draw.address,
                "topics": [event_sig, epoch_topic, word_topic],
                "fromBlock": from_block,
            })
        except Exception as e:
            log.warning(f"fulfill: get_logs failed for ({epoch_id}, {word_id}): {e}")
            return None
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

    def inscribe(self, epoch_id: int, word_id: int) -> str:
        """Mint the Ardinal — only callable if `winner_of(epoch, word) == self.address`."""
        return self._send(
            self._nft.functions.inscribe(epoch_id, word_id),
            gas=300_000,
        )

    def mint_count(self) -> int:
        return int(self._nft.functions.agentMintCount(self.address).call())

    # ----------------------------------------------------------- Fusion --

    def forge_quote(self, token_a: int, token_b: int) -> dict:
        """Read-only LLM oracle preview from Coordinator. No tx."""
        r = self._http.post(
            f"{self.coordinator_url}/v1/forge/quote",
            json={"tokenIdA": token_a, "tokenIdB": token_b, "holder": self.address},
        )
        r.raise_for_status()
        return r.json()

    def forge_sign(self, token_a: int, token_b: int) -> dict:
        """Get a Coordinator-signed fuse authorization. Returns the kwargs
        you'd pass to `fuse()`."""
        r = self._http.post(
            f"{self.coordinator_url}/v1/forge/sign",
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
