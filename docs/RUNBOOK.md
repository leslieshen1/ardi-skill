# Ardi Coordinator — Operations Runbook

This is the on-call playbook for running the Coordinator service in
production. Each section names a symptom and gives the diagnostic +
recovery steps.

## Architecture quick map

```
                       ┌──────────────────┐
                       │  Base mainnet    │
                       │  (RPC endpoint)  │
                       └────────┬─────────┘
                                │
       ┌────────────────────────┼────────────────────────┐
       │                        │                        │
       ▼                        ▼                        ▼
  Indexer loop          Epoch engine             Settlement worker
  (reads events)        (open / publish /        (daily Merkle airdrop)
                         requestDraw)
       │                        │                        │
       └────────────────────────┼────────────────────────┘
                                ▼
                      ┌─────────────────┐
                      │  SQLite DB      │  ← live state, mints, fusion cache
                      └─────────────────┘
                                ▲
                      ┌─────────┴─────────┐
                      │  HTTP API (FastAPI) │
                      │  /v1/epoch/current  │
                      │  /v1/forge/sign     │
                      │  /v1/airdrop/proof  │
                      └─────────────────────┘
```

## Standard incident classification

| Severity | Definition | Response |
|---|---|---|
| **SEV-1** | Coordinator can't sign / publish answers / settle for >15min | Page immediately |
| **SEV-2** | Coordinator partially degraded but still serving | Investigate within 1h |
| **SEV-3** | Cosmetic / non-blocking | Next business day |

---

## Symptom: Coordinator process is dead

**Diagnostic**:
```bash
systemctl status ardi-coordinator     # or docker ps / ps aux
journalctl -u ardi-coordinator -n 200
```

**Recovery**:
1. If OOM-killed: increase memory or restart and watch.
2. If panicked on RPC error: check `cfg.chain.rpc_url` and `rpc_url_backups`
   — the indexer auto-fails over but the chain_writer doesn't. Restart will
   pick up new state from DB.
3. The Coordinator is **idempotent on restart** — it re-reads state from
   SQLite and resumes. There is no separate "leader election"; only run one
   instance at a time.

**State after restart**:
- All open epochs that haven't been answered will be answered as soon as
  the engine ticks (commit window already closed).
- All in-flight VRF requests stay pending — if the chain has fulfilled them,
  the indexer will pick up `WinnerSelected` events and update state.

---

## Symptom: VRF callback never arrives (`drawRequested = true`, `winners[ep][wid] = 0`)

**Cause**: Chainlink subscription unfunded, Coordinator on-call ran out of
LINK/ETH, or Chainlink itself had an outage.

**Diagnostic**:
1. Check Chainlink subscription dashboard:
   `https://vrf.chain.link/base/<subscription_id>`
2. Look for `RandomnessRequested` event on `ChainlinkVRFAdapter` — should
   have a matching `RandomnessFulfilled` ≤ 5 minutes later.

**Recovery**:
1. **Top up the subscription** with LINK or native ETH.
2. After **24 hours** without fulfillment, anyone can call:
   ```solidity
   ArdiEpochDraw(addr).cancelStuckDraw(epochId, wordId);
   ```
   This resets `drawRequested = false`. Then call `requestDraw()` again.
   (Built-in protection — see `DRAW_FULFILLMENT_TIMEOUT = 1 days`).

**Prevention**: monitor `LINK balance < $50` → page on-call.

---

## Symptom: RPC rate-limited or down

**Indicator**: indexer logs `poll error: ...`

**Recovery (automatic)**:
- The indexer auto-fails over to `rpc_url_backups` if configured.
- ChainWriter (which sends openEpoch / publishAnswer / requestDraw) does NOT
  failover automatically — the Coordinator process will crash on RPC error.
  Restart picks up `cfg.chain.rpc_url` again; if that's still down, edit
  config to point at a backup and restart.

**Prevention**: configure at least 2 RPCs (Alchemy + Quicknode + own node):
```toml
[chain]
rpc_url = "https://base.alchemy.com/..."
rpc_url_backups = [
    "https://base.quicknode.com/...",
    "https://mainnet.base.org",  # public
]
```

---

## Symptom: Coordinator missed the publishAnswer window

**Cause**: Coordinator was offline for >submission_window seconds during an
epoch, or RPC errors dropped the publishAnswer tx.

**On-chain effect**:
- `publishAnswer` will revert with `RevealWindowClosed` if attempted late.
- Agents who committed will not be able to reveal (no answer published).
- After reveal window, **agents can sweep their bonds back** via
  `forfeitBond(epoch, wid, agent)` — bond returns to agent because
  `answers[ep][wid].published == false`.

**Recovery**:
- The riddle for that wordId in that epoch becomes a no-op (no winner).
- `consec_unsolved` counter does NOT bump (off-chain Coordinator state
  reflects on-chain truth — no one solved it from the protocol's POV).
- **No data loss**, just one wasted epoch.

**Prevention**: Monitor a counter `ardi_publish_answer_failures_total` and
page if non-zero.

---

## Symptom: Coordinator private key compromised

**This is SEV-0 — protocol-level emergency.**

Immediate actions:
1. **STOP the Coordinator process** to prevent further signed authorizations.
2. **Rotate Coordinator address on chain**:
   - Propose `ArdiNFT.setCoordinator(newAddr)` via Timelock multisig.
   - **Wait the Timelock delay** (48h on mainnet). Yes, this is painful, but
     a shorter delay was a deliberate trade-off (audit C-2 / L-1).
   - During the wait window: the old Coordinator key cannot mint anything
     because `inscribe` is now winner-driven (no Coordinator sig involved).
     Only `fuse` is at risk — but fuse signatures are bound to `holder`
     (audit H-1 fix), so an attacker can't redirect fusions they don't own.
3. After Timelock wait: execute. New Coordinator address active.
4. Generate new key (in HSM/KMS), update config, restart.

**Damage assessment during the 48h wait**:
- `inscribe`: NOT at risk. Winners are determined by VRF on-chain.
- `fuse`: at risk only for tokens the attacker already owns. Attacker can
  forge fusion signatures for their own tokens, but they could already do
  this normally. Net new attack surface: ~0.
- `settleDay`: at risk. Attacker could submit a malicious Merkle root.
  **MITIGATION**: keep `lastSettledDay` monitored and pause claims if a
  suspicious root appears. Holders can refuse to claim until rotation.

**Prevention**: see `docs/SECURITY.md` — store key in HSM/KMS, restrict
network access to Coordinator, rotate quarterly even without compromise.

---

## Symptom: Vault DB corruption / missing rows

**Cause**: Disk full, kernel panic during write, hardware failure.

**Recovery**:
1. Stop Coordinator.
2. Restore from latest daily backup (`pg_dump` / `sqlite3 .backup` —
   see Backup section below).
3. Re-index from chain: delete `*.indexer` DB, restart Coordinator. The
   Indexer will replay `Inscribed` / `Fused` / `Transfer` events from
   block 0 (or `cfg.indexer.start_block` if configured). Takes ~30min for
   100k blocks on Base.
4. `daily_settlement` rows MUST come from backup — they cannot be
   reconstructed from chain alone (the leaves_json is generated locally
   and isn't on-chain). Holders can still claim past airdrops because
   the on-chain Merkle root is intact, but `/v1/airdrop/proof` will 404
   until the row is restored.

**Prevention**:
- Daily SQLite snapshot: `sqlite3 coord.db ".backup /backups/coord-$(date +%F).db"`
- Backups encrypted-at-rest with the same passphrase as the vault file
- Off-site copy

---

## Symptom: Settlement row mismatch (on-chain root != local computation)

**Cause**: Indexer race with settlement worker — settlement read holder
powers while indexer was mid-batch.

**Diagnostic**:
```sql
SELECT day, root, holder_total, fusion_total, tx_hash FROM daily_settlement
ORDER BY day DESC LIMIT 5;
```
Compare `root` column to on-chain `dailyRoots(day).root`. If different →
the settlement_worker computed a Merkle root that doesn't match the chain.

**Recovery**:
- Currently the on-chain root is canonical (signed by Coordinator's
  `settleDay` tx). Local row is just a serving cache for `/v1/airdrop/proof`.
- If they diverge: regenerate locally:
  ```python
  worker.compute_day(day, snapshot_holder_powers)
  ```
  with the EXACT block-height snapshot used at settlement time.

**Prevention**: settlement_worker reads `holder_powers` at
`block_number = lastSettledDay - 1` (TODO: add this snapshot path).

---

## Backup procedures

### Vault encryption file
Located at `cfg.vault.file`. Daily backup, encrypted with passphrase
(stored in operator's password manager, NOT in the same place as the
vault file).

```bash
gpg --symmetric --cipher-algo AES256 \
    --output vault.enc.gpg \
    cfg.vault.file
# Move to off-site (S3 / Backblaze / personal cloud).
```

### SQLite live state
```bash
# Online backup, doesn't lock writers:
sqlite3 coord.db ".backup '/backups/coord-$(date +%F).db'"
```

Also back up `coord.db.indexer`, `coord.db-wal`, `coord.db-shm`
(WAL contents may not be checkpointed to main DB).

### Coordinator private key
Should be in HSM/KMS with rotation policy. If env-var-based for now,
mirror the value in 2-of-3 offline shares (Shamir Secret Sharing).

---

## Pre-mainnet readiness checklist

- [ ] All audit findings addressed or documented as accepted-risk
- [ ] External audit completed
- [ ] Bug bounty live ≥ 4 weeks
- [ ] Testnet Sepolia soak ≥ 2 weeks with realistic load
- [ ] Owner = Timelock + 2-of-3 multisig (not single-key)
- [ ] Coordinator key in HSM/KMS
- [ ] Chainlink subscription funded with at least 30 days of LINK
- [ ] At least 2 RPC providers configured
- [ ] Backup procedure tested (DR drill)
- [ ] On-call rotation defined (≥ 2 people)
- [ ] PagerDuty / Slack alerts wired to monitoring
- [ ] Runbook reviewed by on-call team
- [ ] Frontend live (or "claim only" UI minimum)
- [ ] Discord / docs published

---

## Useful commands

```bash
# Local dev
bash scripts/anvil_commit_reveal_e2e.sh        # full e2e on Anvil
bash scripts/anvil_settlement_e2e.sh           # settlement e2e

# Coordinator
ardi-coordinator --config /etc/ardi/config.toml
curl localhost:8080/v1/health                  # health check
curl localhost:8080/metrics                    # Prometheus

# DB introspection
sqlite3 /var/ardi/coord.db
> .tables
> SELECT * FROM epochs WHERE status='open';
> SELECT day, root, holder_total FROM daily_settlement ORDER BY day DESC LIMIT 5;

# Forge / chain reads
forge script script/Deploy.s.sol --rpc-url $RPC_URL --broadcast
cast call $ARDI_NFT "totalInscribed()(uint256)"
cast call $EPOCH_DRAW "winners(uint256,uint256)(address)" 1 10
cast call $EPOCH_DRAW "pendingRequestsCount()(uint256)"
```
