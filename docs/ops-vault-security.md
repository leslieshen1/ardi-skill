# Vault Security — Ops Runbook

The 21,000 vault answers are the most sensitive artifact in the entire system. If they leak, anyone can win every random draw without solving riddles, and the "intelligence required" promise collapses.

This document is the operational runbook for protecting them.

## Threat model

| Threat | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Pre-launch leak (build host) | Medium | Catastrophic | Encrypted vault file from day one; restricted access |
| Live Coordinator compromise | Medium | High | Encrypted at rest + hash-only memory + audit log |
| Memory dump of running process | Low | High | Hash-only mode + drop plaintext post-seal |
| Logs/database leak answer | Medium | High | Structured audit logger never logs plaintext |
| Insider with operator access | Low | High | KMS / HSM-backed signing key (KeyProvider abstraction in `coordinator/src/coordinator/key_provider.py`); audit-logged reveal path; multi-party operator policy; quarterly key rotation |
| Side-channel via fusion LLM API | Very Low | Low | Fusion only sees minted (already-public) words |

## Defense layers (implementation status)

### ✅ Layer 1: Encrypted at rest

**Implementation**: `coordinator/src/coordinator/secure_vault.py` + `coordinator/scripts/encrypt_vault.py`

- AES-256-GCM authenticated encryption
- PBKDF2-HMAC-SHA256, 600,000 iterations (OWASP 2023+ guidance)
- Random 16-byte salt + 12-byte nonce per file
- Magic prefix `ARDI-VAULT-V1` for format detection

**Pre-launch procedure**:
```bash
cd coordinator/scripts/
export ARDI_VAULT_PASS='<strong passphrase, store in vault manager>'
python3 encrypt_vault.py encrypt \
    --in /path/to/riddles.json \
    --out /path/to/vault.enc

# Verify the encrypted file
python3 encrypt_vault.py decrypt \
    --in /path/to/vault.enc \
    --out /tmp/vault_check.json
diff /path/to/riddles.json /tmp/vault_check.json
shred -u /tmp/vault_check.json     # wipe immediately
```

**Production checklist**:
- [ ] `riddles.json` NEVER copied to Coordinator host. Only `vault.enc` is.
- [ ] `ARDI_VAULT_PASS` is held in a secrets manager (HashiCorp Vault / AWS Secrets Manager / 1Password).
- [ ] On Coordinator startup, secrets manager pushes the passphrase via env. The passphrase is NOT written to disk on the Coordinator host.
- [ ] Passphrase is rotated on a schedule (e.g., quarterly). To rotate: re-encrypt vault with new passphrase, restart Coordinator.

### ✅ Layer 2: Hash-only verification mode

**Implementation**: `SecureVault.verify_guess()` uses `keccak256(NFKC-lowered guess)` compared against pre-computed `answer_hash[word_id]`.

The plaintext word never touches the verify path. So 99% of Coordinator's runtime work doesn't need plaintext at all.

The plaintext map is only consulted by `reveal_word()`, which is called solely by the signer module immediately before producing a mint authorization signature.

**Drop after seal**: once 21,000 originals are minted, all answers are public on-chain (in `Inscribed` events). The Coordinator can call `vault.drop_plaintext()` to free the plaintext map entirely, leaving only hashes for re-publication of stuck riddles (which by then would be minted via Owner rescue path anyway).

### ✅ Layer 3: Audit logging on plaintext access

Every call to `SecureVault.reveal_word(word_id, caller)` is logged via `ardi.vault.audit` logger:

```
INFO ardi.vault.audit reveal_word call #42 caller=epoch.close_and_draw word_id=137 ts=1735604800.123
```

**Critical guarantee**: the actual word string is NEVER in this log line. Only metadata (count, caller, word_id, timestamp).

**Anomaly detection**: query `vault.reveal_stats()` periodically:
```python
{"total_reveals": 142, "last_60s": 3, "last_3600s": 17, "plaintext_remaining": 21000}
```

Healthy: `last_60s ≤ 15` (max 15 mints per epoch, which is also max reveals per epoch). If you see > 15 in a minute or > 100 in an hour, **someone is trying to brute-force vault contents** and the Coordinator should auto-lock.

### Layer 4: Memory hardening (deployment-time)

These are OS-level configurations the operator applies on the Coordinator host. Not implementable in code; this is an ops responsibility.

**On Linux**:

```bash
# 1. Disable swap entirely (so vault never paged to disk)
sudo swapoff -a
# Make permanent in /etc/fstab — comment out swap entries

# 2. Mount /dev/shm with noexec, nosuid (already default on most distros)
mount | grep shm

# 3. Lock decrypted vault into memory (Python equivalent uses ctypes mlock)
# coordinator/src/coordinator/secure_vault.py can be extended to call mlock
# on the plaintext bytes after decrypt — this prevents paging.

# 4. Disable core dumps
echo "* hard core 0" | sudo tee -a /etc/security/limits.conf
sudo sysctl -w kernel.core_pattern=/dev/null

# 5. Disable ptrace from non-root processes
sudo sysctl -w kernel.yama.ptrace_scope=2
```

**On macOS** (dev/staging only — never run prod Coordinator on macOS):
- macOS doesn't support `mlock` reliably in user processes
- For prod, use Linux

### Layer 5: Network isolation

- Coordinator host: dedicated VM/container, single-purpose
- Inbound: only the HTTP API port (8080), TLS-terminated by an upstream load balancer
- Outbound: only Anthropic API + Base RPC + KYA service. Block all other egress.
- SSH: bastion host only, MFA required, time-limited keys.

### Layer 6: Build-time hygiene

- `riddles.json` is in `.gitignore` (already done)
- CI/CD pipelines NEVER print or copy `riddles.json` content
- Build host has the same encryption applied — even the dev environment uses `vault.enc`
- Encrypted file's hash committed in a release manifest so we can detect tampering:
  ```bash
  sha256sum vault.enc > vault.enc.sha256
  git add vault.enc.sha256   # publishable, the hash doesn't reveal answers
  ```

### Layer 7: Coordinator runtime checklist

On startup:

```bash
# Coordinator process startup
1. Read ARDI_VAULT_PASS from secrets manager (NOT from .env file on disk)
2. Call SecureVault(vault_path="/path/to/vault.enc", passphrase=$ARDI_VAULT_PASS)
3. Decrypt happens in /dev/shm tmpfs (allocate buffer via tempfile)
4. Hash all answers immediately, then zero the plaintext bytes
5. Set the plaintext map to be cleared post-seal (drop_plaintext() hook)
6. Confirm /v1/health responds before accepting traffic
7. Periodically: check reveal_stats() for anomalies
```

## Incident response

**If you suspect vault leak**:
1. Immediately stop the Coordinator (`docker compose down` or systemctl stop)
2. Review audit log for unusual `reveal_word` patterns (high frequency, off-hours)
3. Check whether `wordMinted[i]` on-chain is being filled in a pattern that doesn't match published epochs (i.e., someone is winning every draw with no public riddle)
4. If confirmed: coordinate with multisig signers to pause minting via Owner control if such a hook exists, OR accept the partial leak and let mining continue with elevated alerting
5. The vault rotation procedure (regenerate hash root, redeploy NFT contract) is **only available before mainnet seal** — once in production, you cannot rotate without breaking minted Ardinals.

## Future work (V2)

- **TEE / Confidential computing**: run the verify+sign service in AWS Nitro Enclaves or Intel SGX. Vault never decrypts in operator-visible memory.
- **HSM-backed signing key**: Coordinator's ECDSA signing key in YubiHSM / AWS KMS / GCP Cloud HSM. Never extractable.
- **Multi-Coordinator threshold signing**: split the Coordinator across N nodes; require k-of-n threshold signature for each mint authorization. No single host has full vault.
- **ZK riddle solving**: agent submits ZK proof that "I know a string whose hash equals answer_hash[i]" — Coordinator never learns the actual guess.

These are V2 engineering investments. MVP relies on Layers 1-3 (implemented) + 4-7 (operational discipline).
