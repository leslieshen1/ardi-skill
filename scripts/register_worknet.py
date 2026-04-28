#!/usr/bin/env python3
"""
register_worknet.py — Driver for AWP `/api/relay/register-worknet` flow.

Purpose:
  Register Ardi as a worknet on AWP RootNet. Outputs a worknetId that goes
  into Guardian review (3-of-5). Once Guardians approve, AWP runs the
  activation tx which auto-deploys the worknet token (Ardinal / aArdi /
  10B cap), seeds the AWP/aArdi LP (1M AWP × 1B aArdi, locked), and mints
  a WorkNet ID NFT to the operator.

Flow (matches https://docs.awp.work/relay/register-worknet):

  1. POST /api/relay/register-worknet/prepare
       → returns permitTypedData + registerTypedData
  2. Sign both typed-data structs with the deployer wallet (EIP-712).
  3. POST /api/relay/register-worknet
       → relay submits the on-chain registration tx; returns worknetId.
  4. Email hi@agentmail.to with worknetId + Guardian materials
     (scoring.md, audit reports, contract addresses).
  5. Wait for Guardian review (typically 3-7 days).
  6. After activation, run scripts/post_activation_wire.py to grant
     MERKLE_ROLE / OWNER_OPS_ROLE on the now-aligned ArdiMintController.

Required env:
  AWP_RELAY_URL          — base URL of the AWP relay (e.g. https://relay.awp.work)
  AWP_RELAY_API_KEY      — API key for the relay (issued by AWP team)
  AWP_REGISTRATION_PK    — private key of the deployer wallet that holds
                           the 1M AWP registration deposit
  ARDI_SCORING_URL       — public URL to scoring.md (markdown explaining
                           Power, fusion oracle, riddle solving)
  ARDI_SKILLS_URL        — public URL/repo URL to agent-skill/
  ARDI_OPERATOR_ADDR     — operator EOA (where the WorkNet ID NFT goes)

Optional env:
  ARDI_WORKNET_NAME      — defaults to "Ardinal"
  ARDI_WORKNET_SYMBOL    — defaults to "aArdi" (AWP `a`-prefix convention)
  ARDI_WORKNET_DESC      — short description, public-facing

Usage:
  python3 scripts/register_worknet.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests
from eth_account import Account
from eth_account.messages import encode_typed_data


def _env(name: str, default: str | None = None) -> str:
    v = os.environ.get(name, default)
    if v is None or v == "":
        print(f"ERROR: env var {name} is required", file=sys.stderr)
        sys.exit(1)
    return v


def main() -> int:
    relay_url = _env("AWP_RELAY_URL").rstrip("/")
    api_key = _env("AWP_RELAY_API_KEY")
    deployer_pk = _env("AWP_REGISTRATION_PK")
    scoring_url = _env("ARDI_SCORING_URL")
    skills_url = _env("ARDI_SKILLS_URL")
    operator_addr = _env("ARDI_OPERATOR_ADDR")

    name = os.environ.get("ARDI_WORKNET_NAME", "Ardinal")
    symbol = os.environ.get("ARDI_WORKNET_SYMBOL", "aArdi")
    desc = os.environ.get(
        "ARDI_WORKNET_DESC",
        "Multilingual riddle-solving WorkNet. Agents solve riddles, mint Ardinal NFTs via VRF lottery, and earn dual-token rewards (aArdi + AWP).",
    )

    deployer = Account.from_key(deployer_pk)
    print(f"Deployer wallet: {deployer.address}")
    print(f"Relay URL      : {relay_url}")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # --- 1. Prepare ---
    print("\n[1] POST /api/relay/register-worknet/prepare")
    prepare_payload = {
        "deployer": deployer.address,
        "name": name,
        "symbol": symbol,
        "description": desc,
        "operator": operator_addr,
        "scoringUrl": scoring_url,
        "skillsUrl": skills_url,
        # Standard AWP WorkNet defaults — caller may override these via the
        # AWP relay UI later if Guardians want different params.
        "tokenSupplyCap": str(10_000_000_000 * 10**18),  # 10B
        "lpSeedAwp": str(1_000_000 * 10**18),             # 1M AWP
        "lpSeedToken": str(1_000_000_000 * 10**18),       # 1B aArdi
    }
    r = requests.post(
        f"{relay_url}/api/relay/register-worknet/prepare",
        headers=headers,
        json=prepare_payload,
        timeout=30,
    )
    if not r.ok:
        print(f"ERROR: prepare failed: {r.status_code} {r.text}", file=sys.stderr)
        return 2
    prep = r.json()
    permit_typed_data = prep["permitTypedData"]
    register_typed_data = prep["registerTypedData"]

    # --- 2. Sign EIP-712 ---
    print("[2] Signing permit + register typed data (EIP-712)")
    permit_sig = deployer.sign_typed_data(full_message=permit_typed_data).signature.hex()
    register_sig = deployer.sign_typed_data(full_message=register_typed_data).signature.hex()
    if not permit_sig.startswith("0x"):
        permit_sig = "0x" + permit_sig
    if not register_sig.startswith("0x"):
        register_sig = "0x" + register_sig
    print(f"   permit sig   : {permit_sig[:18]}...")
    print(f"   register sig : {register_sig[:18]}...")

    # --- 3. Submit ---
    print("[3] POST /api/relay/register-worknet")
    submit_payload = {
        **prepare_payload,
        "permitSignature": permit_sig,
        "registerSignature": register_sig,
    }
    r = requests.post(
        f"{relay_url}/api/relay/register-worknet",
        headers=headers,
        json=submit_payload,
        timeout=60,
    )
    if not r.ok:
        print(f"ERROR: register failed: {r.status_code} {r.text}", file=sys.stderr)
        return 3
    result = r.json()
    worknet_id = result.get("worknetId")
    tx_hash = result.get("txHash")

    print("\n" + "=" * 60)
    print("REGISTRATION SUBMITTED")
    print("=" * 60)
    print(f"  worknetId : {worknet_id}")
    print(f"  txHash    : {tx_hash}")
    print(f"  status    : {result.get('status', 'Pending')}")

    # Persist for downstream scripts
    out_path = Path(__file__).parent.parent / "deployments" / "awp_registration.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(
        {
            "worknetId": worknet_id,
            "txHash": tx_hash,
            "deployer": deployer.address,
            "operator": operator_addr,
            "name": name,
            "symbol": symbol,
            "scoringUrl": scoring_url,
            "skillsUrl": skills_url,
        },
        indent=2,
    ))
    print(f"\n  saved → {out_path}")

    print("\nNext steps:")
    print("  1. Email hi@agentmail.to with worknetId + scoring.md + audit reports.")
    print("  2. Wait for Guardian (3-of-5) review (3-7 calendar days).")
    print("  3. After activation, run post_activation_wire.py to grant roles")
    print("     on the now-aligned ArdiMintController.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
