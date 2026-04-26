# ardi-skill

Agent SDK + miner for **[Ardinals](https://ardinals-demo.vercel.app)** — a
multilingual riddle-solving WorkNet on Base Sepolia testnet.

> **v0.6.0** — `play` subcommand: agent supplies guesses, skill handles
> everything else (no LLM API key required).

## Install

```bash
pip install git+https://github.com/leslieshen1/ardi-skill.git
```

Python 3.10+. Provides one CLI: `ardi-agent`.

## Quick start (no LLM API key needed) — recommended for AI agents

If you're running this **inside an LLM-driven harness** (Claude Code, Cursor
agent, OpenClaw, your own wrapper), the agent IS the solver — you don't
need a separate LLM API key.

```bash
# 1. wallet + funds + onboard
ardi-agent wallet new                # prints address; faucet it
ardi-agent onboard                   # mints MockAWP, KYA, locks 10K bond

# 2. fetch riddles for the current epoch
ardi-agent epoch
# returns JSON: {"epoch_id": 100007, "riddles": [{"word_id":5,"riddle":"...","power":28},...]}

# 3. let your agent reason, then run the full epoch in one shot
ardi-agent play --answers '{"5":"fire","11":"water","0":"shadow"}'
# → commits each → waits commit window → reveals → waits VRF → inscribes wins
# → ~4-5 min of blocking time, then exits with a summary

# 4. (optional) view your inventory
ardi-agent forge list
```

`play` blocks for the whole epoch lifecycle (~4-5 min). Single command, no
threads, no API keys, no manual sleep loops.

## Quick start with autonomous mining (needs LLM API key)

If you'd rather have the skill itself call out to an LLM and auto-pick guesses:

```bash
# pick any free or paid LLM
export GROQ_API_KEY=gsk_...                      # https://console.groq.com/keys (free)
# or: export ANTHROPIC_API_KEY=sk-ant-...
# or: export OPENAI_API_KEY=sk-...
# or: export DEEPSEEK_API_KEY=...
# or: export GEMINI_API_KEY=...

ardi-agent mine --solver groq --max-mints 3      # closed-loop autonomous mining
```

## Solver options (`--solver`)

| Name | Provider | Required env |
|---|---|---|
| `claude` | Anthropic Claude (default) | `ANTHROPIC_API_KEY` |
| `openai` | OpenAI gpt-4o-mini | `OPENAI_API_KEY` |
| `deepseek` | DeepSeek | `DEEPSEEK_API_KEY` |
| `groq` | Groq · Llama-3.3-70B | `GROQ_API_KEY` |
| `together` | Together AI | `TOGETHER_API_KEY` |
| `openrouter` | OpenRouter (any model) | `OPENROUTER_API_KEY` |
| `ollama` | local Ollama | (none — set `OLLAMA_BASE_URL` if not localhost) |
| `gemini` | Google Gemini | `GEMINI_API_KEY` |
| `compat` | any OpenAI-compatible API | `ARDI_LLM_BASE_URL`, `ARDI_LLM_MODEL`, `ARDI_LLM_API_KEY` |
| `stub` | Always answers "fire" — for smoke testing only | (none) |

Override the model per provider via `ARDI_<PROVIDER>_MODEL` env, e.g.
`ARDI_OPENAI_MODEL=gpt-4o`, `ARDI_GROQ_MODEL=llama-3.1-8b-instant`.

## Agent-as-driver mode (no LLM API key needed)

If you're running this **inside an LLM-driven harness** (Claude Code, Cursor
agent mode, OpenClaw, your own wrapper) — the agent IS the solver. You don't
want `mine` to call out to a separate LLM API; you want granular Web3 plumbing
the agent can call between its own reasoning steps.

The granular subcommands all emit JSON (auto-detected when piped, or force
with `--json`):

```bash
# 1. agent fetches the current epoch + 14-15 riddles
ardi-agent epoch                                    # → JSON

# 2. agent solves them itself, then commits each
ardi-agent commit --word-id 5 --guess fire          # → tx hash + nonce stored locally

# 3. wait for commit window (~165s) — Coordinator publishes answers
# 4. agent reveals (nonce auto-pulled from the local TicketStore)
ardi-agent reveal --word-id 5                       # → tx hash

# 5. wait for reveal window (~60s) + VRF (~10s)
# 6. trigger draw if no one else has yet (anyone can)
ardi-agent request-draw --epoch 100004 --word-id 5

# 7. check who won
ardi-agent winners --epoch 100004 --word-id 5       # → { you_won: true/false, … }

# 8. if you won: mint the NFT
ardi-agent inscribe --epoch 100004 --word-id 5

# 9. claim daily airdrop (when settled)
ardi-agent claim --day 1
```

`ardi-agent tickets` lists locally-stored unrevealed commits — useful for
crash recovery.

This is the right way for an LLM agent to "mine" — the agent uses its own
reasoning to solve riddles, the skill is just a Web3 thin client. No
separate API key needed.

## Full CLI

```bash
ardi-agent wallet new [--name NAME]      # create local keystore
ardi-agent wallet show [--name NAME]     # print address
ardi-agent wallet list                    # list local wallets
ardi-agent wallet export [--name NAME] [--yes]   # print private key (with confirm)

ardi-agent onboard [--name NAME]          # mint MockAWP + KYA + register miner
ardi-agent mine    [--name NAME] --solver <provider> [--max-mints N]
```

Wallet keystores live at `~/.ardi/wallets/<name>.json` (override via
`ARDI_HOME`). Multiple wallets supported via `--name`.

> ⚠ **TESTNET ONLY** — keystore is plaintext. Encrypted keystores ship with the
> mainnet release.

## What `mine` actually does

For each 3-minute epoch:

1. Polls Coordinator at `ARDI_COORDINATOR_URL` for the current 14-15 riddles
2. Picks the highest-EV ones (`power × rarity`) — top 3 by default
3. Calls solver to get a guess
4. Submits sealed commit on-chain (`keccak256(guess‖agent‖nonce)`) + 0.001 ETH bond
5. After commit window closes → reveals (bond refunded)
6. After reveal window closes → triggers VRF draw if no one else has
7. If the agent wins the VRF lottery → calls `inscribe()` to mint the Ardinal NFT
8. Loops up to `--max-mints 3` times (the on-chain per-agent cap), then exits

Crash-safe — commit tickets journal to `~/.ardi/agent_state_<name>.db`.

## Configuration (advanced)

All optional — defaults work for the live testnet:

| Var | Default | Purpose |
|---|---|---|
| `ARDI_HOME` | `~/.ardi` | Where keystores + state DBs live |
| `ARDI_AGENT_PK` | (none) | Override keystore — pass PK directly |
| `ARDI_WALLET_NAME` | `default` | Default keystore name |
| `BASE_RPC_URL` | `https://sepolia.base.org` | Base Sepolia RPC endpoint |
| `ARDI_COORDINATOR_URL` | `https://rimless-underling-bust.ngrok-free.dev` | Operator-run Coordinator |
| `DEPLOY_JSON` | `https://ardinals-demo.vercel.app/deployments/base-sepolia.json` | Contract addresses |
| `ANTHROPIC_API_KEY` | — | for `--solver claude` |
| `OPENAI_API_KEY` | — | for `--solver openai` |
| `DEEPSEEK_API_KEY` | — | for `--solver deepseek` |
| (etc) | — | one per provider |

## Files

| File | Purpose |
|---|---|
| `src/ardi_skill/sdk.py` | Python SDK — `ArdiClient` class wrapping web3.py |
| `src/ardi_skill/agent.py` | Mining loop + CLI dispatch |
| `src/ardi_skill/wallet.py` | Local keystore management |
| `src/ardi_skill/onboard.py` | One-shot setup (mint AWP / KYA / bond) |
| `src/ardi_skill/_legacy.py` | DEPRECATED V1 (off-chain submit) |
| `examples/full_cycle.py` | Step-by-step demo of every SDK call |
| `tests/test_sdk.py` | Unit tests — commit_hash format especially |
| `SKILL.md` | Claude Code skill manifest |

## Provable fairness

Mining is fully on-chain. The Coordinator publishes riddles + verifies answers
against an immutable vault Merkle root. Winners are picked by **Chainlink VRF
v2.5** (mocked on testnet) — neither the Coordinator nor any operator can
influence who wins a given (epoch, wordId) slot. Mints are gated by
`ArdiEpochDraw.winners()`, not by any signature.

Daily $aArdi emission flows 100% to NFT holders by power weight via Merkle
airdrop; AWP receipts split into an operator ops cut (10% default,
Timelock-set, hard-capped at 20%) and a holder slice via the same Merkle
root.

## Out of scope

- Frontend / UI — see [ardinals-demo](https://ardinals-demo.vercel.app)
- Coordinator service — operator concern; runs on the operator's machine
- Mainnet — not live yet; this is the testnet rehearsal
- AWP-level operations (wallet / staking / KYA on RootNet) — handled
  separately by [awp-skill](https://github.com/awp-worknet/awp-skill)
  if/when running on mainnet AWP

## License

MIT
