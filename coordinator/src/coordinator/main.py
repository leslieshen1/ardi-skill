"""Coordinator entry point.

Wires together all modules and runs the FastAPI HTTP server alongside the
async epoch-loop background task.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from . import config as config_mod
from .api import create_app
from .chain_writer import ChainWriter
from .db import DB
from .epoch import EpochEngine
from .forge import Forge
from .fusion import FusionOracle
from .indexer import Indexer
from .kya_bridge import KYABridge
from .secure_vault import SecureVault
from .settlement_worker import SettlementWorker
from .signer import Signer


def cli():
    ap = argparse.ArgumentParser(prog="ardi-coordinator")
    ap.add_argument(
        "--config", default=os.environ.get("ARDI_COORD_CFG", "./config.toml"),
        help="path to config.toml"
    )
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )
    log = logging.getLogger("ardi.main")

    cfg = config_mod.load(args.config)
    log.info(f"loaded config from {args.config}")

    db = DB(cfg.storage.db_path)
    log.info(f"DB at {cfg.storage.db_path}")

    if cfg.vault.encrypted:
        if not cfg.vault.passphrase:
            log.error("ARDI_VAULT_PASS not set; required when vault.encrypted=true")
            sys.exit(1)
        vault = SecureVault(cfg.vault.file, passphrase=cfg.vault.passphrase)
    else:
        log.warning("loading vault in PLAINTEXT mode — DEV ONLY")
        vault = SecureVault(cfg.vault.file, passphrase=None)
    log.info(f"loaded vault: {len(vault)} entries")

    signer = Signer(cfg.coordinator.private_key)
    log.info(f"signer address: {signer.address}")

    fusion = FusionOracle(cfg, db)

    # On-chain writer — sends openEpoch / publishAnswer / requestDraw txs
    # to ArdiEpochDraw on the configured chain. If RPC is down at startup or
    # the contract isn't configured, falls through to dry-run mode (epochs are
    # tracked locally but no on-chain side effects).
    chain_writer = None
    if cfg.contracts.epoch_draw and cfg.chain.rpc_url:
        try:
            chain_writer = ChainWriter(
                rpc_url=cfg.chain.rpc_url,
                private_key=cfg.coordinator.private_key,
                epoch_draw_address=cfg.contracts.epoch_draw,
                chain_id=cfg.chain.chain_id,
            )
            log.info(f"chain writer attached: {chain_writer.address} → {cfg.contracts.epoch_draw}")
        except Exception as e:
            log.warning(f"chain writer setup failed: {e}; running in dry-run mode")

    engine = EpochEngine(cfg, db, vault, chain_writer=chain_writer)
    forge = Forge(cfg, db, signer, fusion)

    # Indexer + settlement worker + KYA bridge (background loops)
    # v1.0: pass epoch_draw_addr too — indexer subscribes to WordCompromised
    # events so epoch.select_riddles can exclude leaked-answer wordIds.
    indexer = Indexer(
        rpc_url=cfg.chain.rpc_url,
        rpc_urls=cfg.chain.rpc_url_backups,  # optional failover list
        ardi_nft_addr=cfg.contracts.ardi_nft,
        epoch_draw_addr=cfg.contracts.epoch_draw,
        db_path=cfg.storage.db_path + ".indexer",
        poll_interval=5,
        confirmation_depth=5,  # Base reorg buffer
    )
    settlement_worker = SettlementWorker(cfg, db, indexer, tick_interval=300)
    kya = KYABridge(cfg)

    app = create_app(cfg, db, vault, engine, fusion, forge)

    import uvicorn

    server_cfg = uvicorn.Config(
        app,
        host=cfg.server.host,
        port=cfg.server.port,
        log_level=args.log_level.lower(),
    )
    server = uvicorn.Server(server_cfg)

    async def run_all():
        await asyncio.gather(
            server.serve(),
            engine.run_loop(),
            indexer.run_loop(),
            settlement_worker.run_loop(),
        )

    try:
        asyncio.run(run_all())
    except KeyboardInterrupt:
        engine.stop()
        indexer.stop()
        settlement_worker.stop()
        log.info("shutdown")


if __name__ == "__main__":
    cli()
