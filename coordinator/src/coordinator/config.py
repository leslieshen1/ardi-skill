"""Configuration loader. Reads TOML + env vars."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]


@dataclass
class ServerCfg:
    host: str
    port: int
    # Trusted reverse-proxy CIDRs. When a request arrives from one of these,
    # the rate-limiter trusts X-Forwarded-For; otherwise it's ignored.
    # Example: ["10.0.0.0/8", "127.0.0.1/32"] for nginx on the same host.
    trusted_proxies: list[str] | None = None


@dataclass
class ChainCfg:
    rpc_url: str
    chain_id: int
    # Optional fallback RPC URLs. If primary fails, indexer + chain reads
    # cycle through these. Empty/omitted = single-RPC mode.
    rpc_url_backups: list[str] | None = None


@dataclass
class ContractsCfg:
    ardi_nft: str
    ardi_token: str
    bond_escrow: str
    mint_controller: str
    otc: str
    awp_token: str
    kya: str
    epoch_draw: str = ""  # ArdiEpochDraw — added in v0.2 for on-chain commit-reveal


@dataclass
class CoordinatorCfg:
    private_key: str  # resolved from env
    sender_pk: str  # resolved from env


@dataclass
class EpochCfg:
    duration_seconds: int
    submission_window: int
    riddles_per_epoch: int
    max_submissions_per_agent: int


@dataclass
class MiningCfg:
    genesis_ts: int
    mining_max_days: int


@dataclass
class VaultCfg:
    file: str                  # path to riddles.json OR vault.enc
    passphrase: str = ""       # decryption passphrase (resolved from env)
    encrypted: bool = False     # if True, treat file as AES-GCM-encrypted


@dataclass
class FusionCfg:
    provider: str
    model: str
    api_key: str  # resolved from env
    cache_dir: str


@dataclass
class SettlementCfg:
    settle_hour_utc: int
    # Legacy single-token bps (kept for backward-compat with old TOMLs and
    # any external dashboards that still read these fields). Under the
    # AWP-aligned manager, $aArdi flows 100% to holders unconditionally;
    # holder_bps / fusion_bps are no longer consulted at settlement time.
    holder_bps: int = 10000
    fusion_bps: int = 0
    # Operator's AWP ops cut, in basis points. Default 10%. Hard-capped on-chain
    # at MAX_OWNER_OPS_BPS = 2000 (20%) — keep in sync with the contract.
    owner_ops_bps: int = 1000


@dataclass
class StorageCfg:
    db_path: str


@dataclass
class Config:
    server: ServerCfg
    chain: ChainCfg
    contracts: ContractsCfg
    coordinator: CoordinatorCfg
    epoch: EpochCfg
    mining: MiningCfg
    vault: VaultCfg
    fusion: FusionCfg
    settlement: SettlementCfg
    storage: StorageCfg


def load(path: str | Path) -> Config:
    raw = tomllib.loads(Path(path).read_text())

    coord_raw = raw["coordinator"]
    pk_env = coord_raw["private_key_env"]
    sender_env = coord_raw["sender_pk_env"]

    fusion_raw = raw["fusion"]
    fusion_key_env = fusion_raw["api_key_env"]

    return Config(
        server=ServerCfg(**raw["server"]),
        chain=ChainCfg(**raw["chain"]),
        contracts=ContractsCfg(**raw["contracts"]),
        coordinator=CoordinatorCfg(
            private_key=os.environ.get(pk_env, ""),
            sender_pk=os.environ.get(sender_env, ""),
        ),
        epoch=EpochCfg(**raw["epoch"]),
        mining=MiningCfg(**raw["mining"]),
        vault=VaultCfg(
            file=raw["vault"]["file"],
            encrypted=raw["vault"].get("encrypted", False),
            passphrase=os.environ.get(
                raw["vault"].get("passphrase_env", "ARDI_VAULT_PASS"), ""
            ),
        ),
        fusion=FusionCfg(
            provider=fusion_raw["provider"],
            model=fusion_raw["model"],
            api_key=os.environ.get(fusion_key_env, ""),
            cache_dir=fusion_raw["cache_dir"],
        ),
        settlement=SettlementCfg(**raw["settlement"]),
        storage=StorageCfg(**raw["storage"]),
    )
