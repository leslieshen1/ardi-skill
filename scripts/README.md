# Ardi scripts

Local utilities and end-to-end test harnesses.

## `e2e_demo.py` — full local integration test

Runs the entire mining loop locally without touching any blockchain:

1. Loads real 21,000-entry vault
2. Spawns Coordinator in-process (epoch engine, signer, vault)
3. Generates N test agent identities (deterministic from index)
4. For each epoch:
   - Coordinator publishes 15 riddles (real rarity weighting)
   - Each agent calls `claude -p sonnet` to solve top-K riddles
   - Submissions persisted to ephemeral SQLite
   - Coordinator closes epoch, runs verifiable random draw
   - Coordinator signs mint authorization for each winner
   - Test verifies every signature recovers to coordinator's address

### Run

```bash
cd ardinals
python3 scripts/e2e_demo.py --epochs 2 --agents 4 --top-k 3 --epoch-duration 30
```

Flags:

| flag | default | meaning |
|---|---|---|
| `--vault` | `data/riddles.json` | path to 21k riddle bank |
| `--epochs` | 2 | how many epochs to simulate |
| `--agents` | 4 | how many simulated agents |
| `--top-k` | 5 | each agent attempts top-K riddles by EV |
| `--epoch-duration` | 30 | seconds per epoch (production = 180) |

### Output (sample)

```
==== Epoch 1/1 ====
opened epoch 1 with 15 riddles
  riddle word_id=6733 lang=de rarity=common power=45
  riddle word_id=21 lang=en rarity=legendary power=74
  ...
  agent0 (0x1a642f0E): submitted 3 guesses
  agent1 (0x5050A4F4): submitted 3 guesses
epoch 1 closed, 2 winners
  Coordinator drew 2 winners
    word_id=16988 word='数据' winner=0x5050a4f4... sig_ok=True
    word_id=21 word='leviathan' winner=0x5050a4f4... sig_ok=True
Epoch 1 done: 2 winners, all signatures valid=True

============================================================
E2E DEMO SUMMARY
============================================================
Epochs run         : 1
Total mint winners : 2
All sigs valid     : True
```

### What this proves

- ✅ Vault loads correctly with all 21,000 entries across 6 languages
- ✅ Epoch publishes the right rarity mix (per design spec §2.2)
- ✅ Hint escalation logic ready (Hint level 0 default)
- ✅ Agents can read riddle JSON, solve via LLM, submit
- ✅ Random draw is deterministic + reproducible
- ✅ Mint authorization signatures recover to coordinator address
- ✅ Same authorization payload would be accepted by `ArdiNFT.inscribe()` on-chain

### What it does NOT test (yet)

- Actual on-chain `ArdiNFT.inscribe()` execution (needs Base testnet deployment)
- KYA bridge attestation reads (needs deployed KYA contract)
- Bond escrow lock/unlock cycles (needs $AWP contract)
- Daily settlement Merkle root submission (needs MintController on-chain)
- Fusion oracle round-trip (needs `ANTHROPIC_API_KEY`)

For chain integration tests, see Phase 0 of `docs/design-spec.md` §7 — requires Base Sepolia deployment.

### Requirements

- Python 3.11+ (or 3.10 with `tomli` installed)
- `claude` CLI installed and authenticated (Claude Code subscription)
- Vault file at `data/riddles.json` (21,000 entries)

### Cost / time

Per epoch with default settings (2 agents, top-K=3):
- ~30 seconds (epoch duration, parallel solve)
- ~10-20 LLM calls total (sonnet)
- $0 if running via Claude Code subscription


## `perf_test.py` — Coordinator throughput benchmark

Stress-tests the engine at the layer that matters: opening epoch, ingesting
N × 5 submissions, closing epoch (filter + random draw + ECDSA sign).

```bash
python3 scripts/perf_test.py --scales 100,1000,5000,10000,50000,100000
```

### Measured throughput (Apple M-series, single thread, SQLite)

| Agents  | Submissions | Submit  | Close+Draw+Sign | Sub/sec | Epoch% |
|--------:|------------:|--------:|----------------:|--------:|-------:|
|     100 |         500 |   4.6ms |          52.5ms |    109k |   0.03%|
|   1,000 |       5,000 |  16.2ms |          64.9ms |    309k |   0.05%|
|   5,000 |      25,000 |  66.1ms |          93.8ms |    378k |   0.09%|
|  10,000 |      50,000 |   128ms |           131ms |    391k |   0.14%|
|  50,000 |     250,000 |   718ms |           784ms |    348k |   0.83%|
| 100,000 |     500,000 |  1.58 s |          2.02 s |    316k |   2.00%|

**At 100,000 concurrent agents**: full hot path completes in 3.6 seconds — 2% of
the 180-second epoch budget. Coordinator has ~50× headroom over realistic peak
agent populations.

### Phase breakdown

- `submit`: bulk INSERT to SQLite of all agent submissions
- `close+draw+sign`: filter correct guesses, run keccak-based random draw per
  riddle, ECDSA-sign 15 mint authorizations
- `verify`: recover signer from each authorization (sanity check, off the
  hot path)

### Bottleneck analysis

Coordinator is **not** the bottleneck. Real-world bottlenecks:

- **Base chain throughput**: ~50 TPS — fine because only 15 mints per 3-min epoch
- **Agent-side LLM latency**: ~30s per agent per epoch (claude/gpt round-trip)
- **HTTPS RTT**: 1-3s per submission

Even at 100k concurrent agents over the full ~70-hour mining period, total
Coordinator CPU time is only ~84 minutes.

