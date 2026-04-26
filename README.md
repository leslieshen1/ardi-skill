# ardi-skill

The agent SDK + reference miner for **[Ardinals](https://ardinals-demo.vercel.app)** —
a multilingual riddle-solving WorkNet on Base Sepolia testnet.

> **v0.3.0** · multi-LLM solver · skill-driven onboarding · single CLI

## Install

```bash
pip install git+https://github.com/leslieshen1/ardi-skill.git
```

Python 3.10+. Provides one CLI: `ardi-agent`.

## 30-second quick start

```bash
# 1. Make a wallet (saved at ~/.ardi/wallets/default.json — testnet only)
ardi-agent wallet new

# It prints your address. Copy it.

# 2. Get Base Sepolia ETH for gas (any of these faucets):
#    https://portal.cdp.coinbase.com/products/faucet
#    https://www.alchemy.com/faucets/base-sepolia
#    https://faucet.quicknode.com/base/sepolia

# 3. One-shot setup: self-mint MockAWP, verify on MockKYA, lock 10K bond
ardi-agent onboard

# 4. Pick an LLM solver and start mining
export ANTHROPIC_API_KEY=sk-ant-...
ardi-agent mine --solver claude --max-mints 3
```

That's the whole flow. **Every step is real on-chain** — addresses, balances,
and tx hashes are visible on [Basescan Sepolia](https://sepolia.basescan.org/).

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
