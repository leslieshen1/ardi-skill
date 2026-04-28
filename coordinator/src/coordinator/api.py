"""HTTP API exposed by Coordinator under the on-chain commit-reveal architecture.

The Coordinator no longer accepts agent submissions over HTTP. Submissions are
made on-chain via ArdiEpochDraw.commit() / .reveal(); the Coordinator only
publishes:
  - GET  /v1/epoch/current             — current epoch's published riddles + chain identifiers
  - GET  /v1/epoch/{id}                 — historical epoch (read-only)
  - GET  /v1/agent/{addr}/state         — agent's mint state from indexer
  - POST /v1/forge/quote                 — fusion oracle preview (LLM read-only)
  - POST /v1/forge/sign                  — signed fusion authorization
  - GET  /v1/forge/record/{tokenId}     — fusion lore lookup
  - GET  /v1/airdrop/proof/{day}/{agent} — Merkle airdrop proof
  - GET  /v1/health                      — health check
"""
from __future__ import annotations

import json
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .config import Config
from .db import DB
from .epoch import EpochEngine
from .forge import Forge, ForgeError
from .fusion import FusionOracle
from .metrics import metrics as _shared_metrics
from .middleware import TokenBucketLimiter, install_metrics, install_rate_limit
from .secure_vault import SecureVault


# Module-scope so `from __future__ import annotations` + FastAPI's ForwardRef
# resolution can find this class. Defining it inside create_app() turns the
# annotation into a ForwardRef('ForgeRequest') that resolves against globals,
# but the actual class lives in local scope — pydantic falls back to treating
# it as a query param (loc=['query','req']) and every POST gets a 422.
class ForgeRequest(BaseModel):
    tokenIdA: int
    tokenIdB: int
    holder: str


def create_app(
    cfg: Config,
    db: DB,
    vault: SecureVault,
    engine: EpochEngine,
    fusion: FusionOracle,
    forge: Forge,
) -> FastAPI:
    app = FastAPI(title="Ardi Coordinator", version="0.2.0")

    # CORS — the demo frontend lives at https://ardinals-demo.vercel.app
    # but anyone can stand up their own dashboard pointing at this Coordinator.
    # On testnet we accept all origins; production deploys can tighten via
    # cfg.server.cors_origins (a list) once we wire it through.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=getattr(cfg.server, "cors_origins", None) or ["*"],
        allow_credentials=False,   # *-origin + credentials is forbidden by spec
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        max_age=600,
    )

    # Catch-all 500 handler that returns JSON with CORS headers. Without
    # this, an unhandled exception in an endpoint (e.g. a contract revert
    # web3 didn't translate cleanly) bubbles up to uvicorn's default 500
    # response, which emits NO CORS headers — the browser then reports
    # "Failed to fetch" instead of letting JS see the actual error.
    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        # Re-raise HTTPException so FastAPI's normal handler runs (it
        # already returns proper JSON + CORS).
        if isinstance(exc, HTTPException):
            raise exc
        return JSONResponse(
            status_code=500,
            content={"detail": f"internal error: {type(exc).__name__}: {str(exc)[:200]}"},
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            },
        )

    # Default tier: 60 req/IP/60s burst for /v1/* paths
    # Forge tier: 6 req/IP/3min burst (LLM-expensive)
    limiter = TokenBucketLimiter(capacity=60, refill_per_sec=1.0)
    forge_limiter = TokenBucketLimiter(capacity=6, refill_per_sec=1.0 / 30.0)
    install_rate_limit(
        app, limiter,
        forge_limiter=forge_limiter,
        trusted_proxies=cfg.server.trusted_proxies,
    )

    # Use the module-level singleton so other components (epoch loop,
    # indexer, settlement worker) write to the same Metrics instance the
    # /metrics endpoint exposes.
    metrics = _shared_metrics
    install_metrics(app, metrics)

    @app.get("/v1/health")
    def health():
        return {"ok": True, "vaultSize": len(vault), "ts": int(time.time())}

    @app.get("/v1/epoch/current")
    def current_epoch():
        state = engine.get_open_epoch()
        if not state:
            raise HTTPException(status_code=404, detail="no open epoch")
        return {
            "epochId": state.epoch_id,
            "startTs": state.start_ts,
            "commitDeadline": state.commit_deadline,
            "revealDeadline": state.reveal_deadline,
            # Chain identifiers — agents use these to compute on-chain commit hashes:
            #   commit = keccak256(abi.encodePacked(guess, msg.sender, nonce))
            # and to know which contract to call.
            "chainId": cfg.chain.chain_id,
            "epochDrawContract": cfg.contracts.epoch_draw,
            "ardiNftContract": cfg.contracts.ardi_nft,
            "riddles": [
                {
                    "wordId": r.word_id,
                    "riddle": r.riddle,
                    "power": r.power,
                    "rarity": r.rarity,
                    "language": r.language,
                    "languageId": r.language_id,
                    "hintLevel": r.hint_level,
                }
                for r in state.riddles
            ],
        }

    @app.get("/v1/epoch/{epoch_id}")
    def epoch_by_id(epoch_id: int):
        with db.conn() as c:
            row = c.execute(
                "SELECT epoch_id, start_ts, commit_deadline, reveal_deadline, riddles, status, "
                "open_tx, publish_tx, request_tx FROM epochs WHERE epoch_id = ?",
                (epoch_id,),
            ).fetchone()
        if not row:
            raise HTTPException(status_code=404)
        return dict(row)

    @app.get("/v1/agent/{addr}/state")
    def agent_state(addr: str):
        with db.conn() as c:
            mints = c.execute(
                "SELECT word_id, token_id, epoch_id, minted_at FROM mints WHERE agent = ?",
                (addr.lower(),),
            ).fetchall()
        return {
            "agent": addr,
            "mints": [dict(m) for m in mints],
            "mintCount": len(mints),
            "remainingMintCap": max(0, 3 - len(mints)),
        }

    @app.post("/v1/forge/quote")
    async def forge_quote(req: ForgeRequest):
        """Read-only fusion preview — no signature, no DB write."""
        try:
            return await forge.quote(req.tokenIdA, req.tokenIdB, req.holder)
        except ForgeError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except RuntimeError as e:
            # LLM oracle transient failures (rate limit, content filter,
            # session unhealthy). 503 = "try again later" — proper for
            # this kind of upstream-flakiness problem.
            raise HTTPException(status_code=503, detail=f"oracle unavailable: {str(e)[:200]}")

    @app.post("/v1/forge/sign")
    async def forge_sign(req: ForgeRequest):
        """Full forge authorization. Reads chain to verify ownership + nonce,
        calls LLM oracle, signs the V2 fuse authorization (binds holder),
        persists fusion record. Returns a signature the holder can submit
        to ArdiNFT.fuse()."""
        try:
            return await forge.sign(req.tokenIdA, req.tokenIdB, req.holder)
        except ForgeError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except RuntimeError as e:
            raise HTTPException(status_code=503, detail=f"oracle unavailable: {str(e)[:200]}")

    @app.get("/v1/forge/record/{token_id}")
    def forge_record(token_id: int):
        """Lookup the fusion lore for a given Ardinal tokenId.

        - For fusion products (tokenId ≥ 21001): returns the parent words,
          LLM compatibility score, suggested word, and the rationale.
        - For originals (tokenId 1..21000): returns the riddle that earned it.
          Word is revealed only AFTER the on-chain Inscribed event is indexed.
        - Otherwise 404.
        """
        if token_id <= 21_000:
            word_id = token_id - 1
            if word_id < 0 or word_id >= len(vault):
                raise HTTPException(status_code=404)
            entry = vault.get_entry(word_id)

            with db.conn() as c:
                row = c.execute(
                    "SELECT 1 FROM mints WHERE word_id = ? LIMIT 1", (word_id,)
                ).fetchone()
                is_minted = row is not None

            response = {
                "tokenId": token_id,
                "type": "original",
                "language": entry.language,
                "power": entry.power,
                "rarity": entry.rarity,
                "lore": {
                    "riddle": entry.riddle,
                    "description": (
                        f"This Ardinal was earned by an AI agent who solved the riddle: "
                        f'"{entry.riddle}"'
                    ),
                },
            }
            if is_minted:
                response["word"] = vault.reveal_word(word_id, caller="api.lore_minted")
            return response

        # Fusion product
        with db.conn() as c:
            row = c.execute(
                "SELECT word_a, word_b, success, new_word, new_power, new_lang, "
                "compatibility, rationale, holder, timestamp "
                "FROM fusion_records WHERE new_token = ?",
                (token_id,),
            ).fetchone()
        if not row:
            raise HTTPException(status_code=404)

        return {
            "tokenId": token_id,
            "type": "fusion",
            "word": row["new_word"],
            "power": row["new_power"],
            "languageId": row["new_lang"],
            "lore": {
                "parents": [{"word": row["word_a"]}, {"word": row["word_b"]}],
                "compatibility": row["compatibility"],
                "rationale": row["rationale"],
                "description": (
                    f"This Ardinal emerged from the fusion of "
                    f'"{row["word_a"]}" and "{row["word_b"]}". '
                    f"Compatibility: {row['compatibility']:.2f}. "
                    f"{row['rationale']}"
                ),
                "fusedAt": row["timestamp"],
                "fusedBy": row["holder"],
            },
        }

    @app.get("/v1/airdrop/proof/{day}/{agent}")
    def airdrop_proof(day: int, agent: str):
        """Return the dual-token airdrop proof for `agent` on `day`.

        Leaves are stored as {addr: [ardi_amount, awp_amount]}. The proof
        verifies against keccak256(abi.encodePacked(addr, ardi, awp)) — the
        same leaf format the on-chain ArdiMintController.claim() expects.
        """
        with db.conn() as c:
            row = c.execute(
                "SELECT root, leaves_json FROM daily_settlement WHERE day = ?", (day,)
            ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="day not settled")

        leaves_raw = json.loads(row["leaves_json"])
        target = agent.lower()
        if target not in {k.lower() for k in leaves_raw.keys()}:
            raise HTTPException(status_code=404, detail="agent not in tree")

        # Normalize legacy single-token leaves (pre-AWP-alignment) to the
        # dual-token shape with awp=0 so the same endpoint serves old + new days.
        leaves: dict[str, tuple[int, int]] = {}
        for addr, v in leaves_raw.items():
            if isinstance(v, list) and len(v) == 2:
                leaves[addr] = (int(v[0]), int(v[1]))
            else:
                # Legacy single-token row — interpret amount as $aArdi only.
                leaves[addr] = (int(v), 0)

        from .merkle import build_dual_airdrop_tree
        _, proofs = build_dual_airdrop_tree(leaves)
        for addr, (ardi_amt, awp_amt) in leaves.items():
            if addr.lower() == target:
                return {
                    "day": day,
                    "agent": addr,
                    "ardiAmount": str(ardi_amt),
                    "awpAmount": str(awp_amt),
                    "proof": ["0x" + p.hex() for p in proofs.get(addr, [])],
                    "root": row["root"],
                }
        raise HTTPException(status_code=404)

    return app
