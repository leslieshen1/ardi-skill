"""Local wallet manager for ardi-skill.

Stores keystores under ~/.ardi/wallets/<name>.json (or wherever ARDI_HOME
points). Single-tenant: one wallet per name. Encryption is intentionally
left off for testnet ergonomics — the keystore JSON contains the raw
private key. **DO NOT REUSE TESTNET KEYS ON MAINNET.**

Migrating to encrypted keystores (BIP-39 mnemonic + AES-GCM) is on the
roadmap once we go to mainnet; for now the CLI prints a loud red warning
when it writes a key.

Public CLI commands (wired in agent.py):
  ardi-agent wallet new [--name NAME]
  ardi-agent wallet show [--name NAME]
  ardi-agent wallet export [--name NAME] [--yes]
  ardi-agent wallet list
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Optional

from eth_account import Account


def _home() -> Path:
    """The directory holding all ardi-skill local state.

    Override with ARDI_HOME env var. Default: ~/.ardi
    """
    p = Path(os.environ.get("ARDI_HOME", str(Path.home() / ".ardi")))
    p.mkdir(parents=True, exist_ok=True)
    return p


def wallets_dir() -> Path:
    p = _home() / "wallets"
    p.mkdir(parents=True, exist_ok=True)
    return p


def wallet_path(name: str) -> Path:
    if not name or "/" in name or ".." in name:
        raise ValueError(f"invalid wallet name: {name!r}")
    return wallets_dir() / f"{name}.json"


def list_wallets() -> list[str]:
    return sorted(p.stem for p in wallets_dir().glob("*.json"))


def create_wallet(name: str = "default") -> dict:
    path = wallet_path(name)
    if path.exists():
        raise FileExistsError(f"wallet {name!r} already exists at {path}")
    pk = "0x" + secrets.token_hex(32)
    acct = Account.from_key(pk)
    data = {
        "name": name,
        "address": acct.address,
        "private_key": pk,
        "created_at": int(time.time()),
        "version": 1,
        "warning": "TESTNET ONLY — plaintext key, do not use on mainnet",
    }
    path.write_text(json.dumps(data, indent=2))
    path.chmod(0o600)
    return data


def load_wallet(name: str = "default") -> dict:
    path = wallet_path(name)
    if not path.exists():
        existing = list_wallets()
        hint = f" available: {', '.join(existing)}" if existing else " (none yet)"
        raise FileNotFoundError(f"no wallet {name!r} at {path}.{hint}")
    return json.loads(path.read_text())


def get_address(name: str = "default") -> str:
    return load_wallet(name)["address"]


def get_private_key(name: str = "default") -> str:
    return load_wallet(name)["private_key"]


# ----- CLI handlers (wired into the argparse subcommands in agent.py) -----

def cmd_wallet_new(args):
    try:
        data = create_wallet(args.name)
    except FileExistsError as e:
        print(f"✗ {e}", file=sys.stderr)
        sys.exit(2)
    print(f"\n✓ Created wallet {args.name!r}")
    print(f"   address     : {data['address']}")
    print(f"   keystore    : {wallet_path(args.name)}")
    print(f"\n⚠  TESTNET ONLY — keystore is plaintext. Do not put real funds here.\n")
    print("Next steps:")
    print(f"   1. Get Base Sepolia ETH: https://portal.cdp.coinbase.com/products/faucet")
    print(f"      Paste this address: {data['address']}")
    print(f"   2. Run:  ardi-agent onboard --name {args.name}")
    print(f"   3. Run:  ardi-agent mine   --name {args.name} --solver claude")


def cmd_wallet_show(args):
    try:
        data = load_wallet(args.name)
    except FileNotFoundError as e:
        print(f"✗ {e}", file=sys.stderr)
        sys.exit(2)
    print(f"name      : {data['name']}")
    print(f"address   : {data['address']}")
    print(f"keystore  : {wallet_path(args.name)}")
    print(f"created   : {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(data['created_at']))}")
    print(f"basescan  : https://sepolia.basescan.org/address/{data['address']}")


def cmd_wallet_list(args):
    names = list_wallets()
    if not names:
        print("no wallets yet — run `ardi-agent wallet new` to create one")
        return
    print(f"{'NAME':<20}  {'ADDRESS':<44}")
    for n in names:
        try:
            d = load_wallet(n)
            print(f"{n:<20}  {d['address']}")
        except Exception as e:
            print(f"{n:<20}  (error: {e})")


def cmd_wallet_export(args):
    try:
        data = load_wallet(args.name)
    except FileNotFoundError as e:
        print(f"✗ {e}", file=sys.stderr)
        sys.exit(2)
    if not args.yes:
        print(f"\n⚠  About to print the PRIVATE KEY for wallet {args.name!r}.")
        print(f"   address : {data['address']}")
        print(f"   anyone with this key controls this wallet.")
        confirm = input("\nType the wallet name again to confirm: ").strip()
        if confirm != args.name:
            print("✗ confirmation mismatch — aborted")
            sys.exit(1)
    print(f"\n{data['private_key']}")


# ----- Helper for downstream commands (onboard, mine) to resolve PK -----

def resolve_private_key(args_name: Optional[str]) -> tuple[str, str]:
    """Return (address, private_key). Order of resolution:
    1. ARDI_AGENT_PK env var (legacy direct mode)
    2. --name argument → keystore lookup
    3. ARDI_WALLET_NAME env var → keystore lookup
    4. wallet 'default' (if it exists)
    """
    # 1. Env override
    pk_env = os.environ.get("ARDI_AGENT_PK")
    if pk_env:
        acct = Account.from_key(pk_env)
        return (acct.address, pk_env)

    # 2-3. Named keystore
    name = args_name or os.environ.get("ARDI_WALLET_NAME") or "default"
    try:
        data = load_wallet(name)
        return (data["address"], data["private_key"])
    except FileNotFoundError:
        existing = list_wallets()
        if existing:
            raise SystemExit(
                f"no wallet {name!r}. Run `ardi-agent wallet new --name {name}` "
                f"or pass --name {existing[0]} (existing: {', '.join(existing)})"
            )
        raise SystemExit(
            "no wallet configured. Run `ardi-agent wallet new` first, "
            "or set ARDI_AGENT_PK."
        )
