# Ardi WorkNet — Design Specification

> Implementation-oriented spec. Distinct from `aip-003-v3.md` (governance proposal). This doc describes how the WorkNet actually gets built, parameter by parameter, component by component.

---

## 0. North Star

**21,000 multilingual words. Each becomes a scarce on-chain inscription only when an AI agent proves it understands the word behind a riddle.** No human mints. No script mints. The output is the first multilingual on-chain dictionary authored entirely by machine intelligence.

---

## 0.5. Provable Fairness

Ardi is built so that **no single party — including the Owner team — can
preferentially mint, win, or claim**. Every fairness-critical step is on
chain and externally verifiable:

| Step | Who decides | How it's verified |
|---|---|---|
| Vault contents | Fixed at deploy via `VAULT_MERKLE_ROOT` (immutable) | Anyone can verify a published answer's inclusion via Merkle proof against the on-chain root |
| Riddle commitment | Each agent posts `keccak256(guess‖agent‖nonce)` on-chain BEFORE the answer is published | Mempool only sees the hash; the guess is sealed |
| Answer publication | Coordinator posts the canonical answer on-chain AFTER the commit window closes, accompanied by a Merkle proof against `VAULT_MERKLE_ROOT` | Reverts if the published `(word, power, languageId)` is not in the vault — Coordinator cannot publish a non-canonical answer |
| Winner selection | Chainlink VRF v2.5 over the on-chain set of correct revealers | VRF proof is recorded on-chain and replayable; Coordinator has no influence on the random word |
| Mint authorization | Pure on-chain check `ArdiNFT.inscribe(epoch, wordId)` requires `ArdiEpochDraw.winners(epoch, wordId) == msg.sender` | No Coordinator signature is involved or accepted |
| $aArdi emission | 100% to holders, by power weight, via Merkle airdrop | Per-day root + totals on-chain; anyone can verify their entry |
| AWP receipt | Operator share `ownerOpsBps` (≤ 20%) + remainder to holders by the same power weight; same Merkle root | `ownerOpsBps` is Timelock-set with a hard 20% on-chain cap; per-day totals + reserves are on-chain |
| Slash flow | Triggered by KYA contract or owner; 50% burned, 50% to treasury | All transfers logged on-chain |
| Owner powers | Bounded to non-mint operations (rotate Coordinator address, set ownerOpsAddr/Bps, swap Randomness source) and gated by 48h Timelock + multisig | Every owner action is a public Timelock proposal with a 48h cancel window |

The Coordinator's only authoritative role at run-time is **publishing
Merkle-verified canonical answers**. It cannot pick winners. It cannot
mint. It cannot redirect emission. The on-chain contracts enforce all
of this — see `contracts/src/ArdiEpochDraw.sol` and
`contracts/src/ArdiNFT.sol` for the actual code.

What this means for participants:
- **Mining**: anyone with a registered AWP wallet + KYA + 10K $AWP bond can
  participate. Same odds as anyone else, given equal solving ability.
- **Holding**: $ardi flows to holders proportionally to power, no team
  carve-out, no insider channel.
- **Fusion**: outcomes follow a published rubric (see §3.x); same
  oracle for everyone, deterministic at temperature=0, cached + versioned.

---

## 1. Vault — the immutable wordbank

### 1.1 Composition

| Language | Count | Notes |
|---|---:|---|
| English (en) | 6,000 | mainstream + crypto vocab |
| Chinese (zh) | 5,000 | simplified |
| Japanese (ja) | 3,000 | kanji + kana |
| Korean (ko) | 3,000 | hangul |
| French (fr) | 2,000 | accented dictionary forms |
| German (de) | 2,000 | accented dictionary forms |
| **Total** | **21,000** | |

### 1.2 Per-entry schema

```json
{
  "id": 0,
  "word": "bitcoin",
  "language": "en",
  "riddle": "Digital gold forged in computational fire...",
  "power": 100,
  "rarity": "legendary",
  "difficulty": 100
}
```

- **id**: 0 to 20,999 (also serves as `wordId` on-chain)
- **word**: written in target language
- **riddle**: English prose (works for all 6 languages — describes a target-language word)
- **power**: 1-100 (heat / cultural weight, drives airdrop weight)
- **rarity**: derived from power (see thresholds below)
- **difficulty**: 2-100 (informational, not used by contract)

### 1.3 Power thresholds → rarity

| Rarity | Power range | Count | Share |
|---|---|---:|---:|
| legendary | ≥ 74 | ~1,117 | 5.3% |
| rare | 62-73 | ~3,790 | 18.0% |
| uncommon | 52-61 | ~4,790 | 22.8% |
| common | < 52 | ~11,303 | 53.8% |

Pyramid distribution. `bitcoin = 100`, `god = 99`, `dream = 92`, `比特币 = 98`.

### 1.4 Immutability

- Generated and curated pre-launch (already done in `wordbank-builder/riddles.json`)
- Quality validated through opus + sonnet blind-answering + coherence judgment + iterative rewrites
- **Merkle root** of all 21,000 entries written to contract at deployment
- After deployment: vault is sealed. Coordinator publishes riddles selectively but cannot alter content.

### 1.5 Riddle-only public exposure

Agents see only `(id, language, riddle, power, rarity)`. The `word` field is hidden until minted. Coordinator stores `word` server-side; only reveals via mint signature.

---

## 2. Mining — riddle epochs

### 2.1 Epoch parameters

| Parameter | Value |
|---|---|
| Epoch duration | **3 minutes** |
| Riddles published per epoch | **15** |
| Submission deadline | epoch start + 2 min 45 sec |
| Reveal & mint window | last 15 sec |

### 2.2 Per-epoch puzzle distribution

Each epoch's 15 riddles are weighted to mirror the bank's rarity distribution:

| Slots | Source rarity |
|---|---|
| 1-7 | common |
| 8-10 | uncommon |
| 11-13 | uncommon (60%) / rare (40%) |
| 14 | rare (70%) / legendary (30%) |
| 15 | rare (50%) / legendary (50%) |

Coordinator draws specific riddles from unsolved pool weighted by rarity.

### 2.3 Per-epoch language distribution

15 riddles split roughly proportionally to vault language shares:

| Language | Slots per epoch (typical) |
|---|---|
| en | 4-5 |
| zh | 3-4 |
| ja | 2 |
| ko | 2 |
| fr | 1-2 |
| de | 1-2 |

Coordinator may adjust per epoch to preserve solve diversity.

### 2.4 Submission rules

| Rule | Value |
|---|---|
| Submissions per agent per epoch | **up to 5** |
| Max Ardinals per agent (lifetime) | **3** |
| Incorrect submissions | no penalty, no slash |
| Correct but not drawn | recorded for reputation, no $ardi |

Once an agent has minted 3 Ardinals, they can no longer submit. Their stake unlocks immediately (see §4).

### 2.5 Win condition (per riddle, per epoch)

```
1. Collect all submissions for riddle R from agents within deadline
2. Filter: word match must be exact (NFKC normalized)
3. Branch on outcome:
   a. If ≥ 1 correct submission → run verifiable random draw → 1 winner mints Ardinal R
   b. If 0 correct submissions → riddle returns to unsolved pool; consecUnsolved[R]++
4. Coordinator does NOT publicly mark which riddles are stuck
   (avoid flocking effects in subsequent epochs)
```

**Random seed** (V2, blockhash-grounded, ungrindable):
```
seed = keccak256(
    "ARDI_DRAW_V2"            ||
    chainId                    ||  // 32 B — prevents cross-chain replay
    contract                   ||  // 20 B — binds to ArdiNFT instance
    epoch_id                   ||  // 8 B
    word_id                    ||  // 8 B
    blockhash(close_block + 1) ||  // 32 B — UNPREDICTABLE at epoch close
    keccak(sorted(agent_addrs))    // 32 B — commits to participant set
)
winner_index = uint256(seed) % N
```

**Fairness guarantees**:

1. **Coordinator cannot grind**. The blockhash of `close_block + 1` is unknown
   when Coordinator commits to the agent set. Any change to either the agent
   set or the future blockhash flips the winner unpredictably.
2. **Replay-immune**. Versioned prefix + chainId + contract address bind the
   draw to one specific epoch on one specific chain.
3. **Order-invariant**. Coordinator can't manipulate winner by re-ordering
   the agent list — the draw module sorts internally.
4. **Publicly verifiable**. Anyone can re-run the draw via
   `POST /v1/draw/verify` with the original inputs and confirm the claimed
   winner matches.

**Adversary analysis**:

| Attacker | Capability | Defense |
|---|---|---|
| Coordinator (honest input + grinding) | Pick favorable agent set ordering | Sorted internally — order doesn't matter |
| Coordinator (dishonest input) | Add fake correct submissions | Each agent's submission is signed by their wallet — adding fakes requires forging signatures |
| Coordinator (delay attack) | Wait until favorable blockhash before publishing | Submission deadline locks the agent set; Coordinator must commit before next blockhash |
| Sequencer (Base) | Selectively reorder/include block to influence blockhash | One block = one chance per riddle. Cost > prize for all but legendary; even then no guarantee. |
| Agent | Predict blockhash early | Cryptographically infeasible without 51% control |

### 2.5.1 Re-publication policy for unsolved riddles

- Returned to general unsolved pool
- Eligible to be drawn for any future epoch per normal rarity weighting
- NOT forced into the next epoch (avoids one bad riddle flooding the queue)
- Tracked via `consecUnsolved[wordId]` counter for stuck-detection (see §8.1)

### 2.6 Coordinator vs contract responsibility split

| Action | Coordinator (off-chain) | Contract (on-chain) |
|---|---|---|
| Publish riddle (id, riddle, power, rarity, language) | ✅ | — |
| Receive encrypted submissions | ✅ | — |
| Verify word match | ✅ | — |
| Compute random seed and winner | ✅ (computed) | ✅ (verifiable) |
| Sign mint authorization | ✅ | — |
| Verify signature & mint NFT | — | ✅ |
| Update `wordMinted[id]` mapping | — | ✅ |
| Update `agentMintCount[addr]` | — | ✅ |

---

## 3. Forge — fusion mechanics

### 3.1 Concept

Holder brings two Ardinals they own into the Forge. LLM oracle (`temperature=0`, deterministic) evaluates semantic compatibility. Output drives outcome.

### 3.2 Compatibility & rewards

```
compatibility ∈ [0, 1]
success_rate = 0.20 + compatibility × 0.50    # range 20% - 70%
power_multiplier = inverse_relation(compatibility)
```

| Compatibility | Success rate | Power multiplier |
|---|---|---|
| > 0.8 | 60-70% | 1.5× |
| 0.6 - 0.8 | 50-60% | 2.0× |
| 0.3 - 0.6 | 35-50% | 2.5× |
| < 0.3 | 20-35% | 3.0× |

### 3.3 Outcomes

**Success**:
- Burn both parents
- Mint new Ardinal with `word = LLM_suggested_word`, `power = (powerA + powerB) × multiplier`
- New tokenId = 21,000 + ++fusionCount
- Generation = max(genA, genB) + 1
- Parents linked on-chain (full lineage traceable)

**Failure**:
- Burn the lower-Power parent
- The higher-Power parent is preserved (no penalty beyond losing one token)

### 3.4 Multilingual fusion (open design, decide before launch)

Cross-language fusions present interesting cases:
- `fire (en, P=94)` + `火 (zh, P=88)` → high compatibility (same concept across cultures)
- `dream (en, P=92)` + `夢 (ja, P=90)` → high compatibility
- `bitcoin (en, P=100)` + `比特币 (zh, P=98)` → identical concept

**Decision needed**: do we allow same-concept different-language fusion? If yes, output language is what?

**Recommendation**: allow cross-lingual fusion. New word's language is randomly weighted by parent powers (higher power parent's language wins the coin flip).

### 3.5 Cooldown & constraints

| Constraint | Value |
|---|---|
| Ownership | both Ardinals on same address |
| Per-address cooldown | 24 hours |
| Generations | unlimited |
| Available during mining | yes (no need to wait for sealed) |
| Gas | TBD (LLM oracle call adds latency) |

### 3.6 LLM oracle determinism

- Model: TBD (Claude Sonnet or Opus, fixed version)
- Temperature: 0
- Output cached on-chain by `(min(idA, idB), max(idA, idB))` to make repeats free
- Prompt: standardized, version-pinned

```
SYSTEM: Evaluate semantic compatibility between two words across (possibly different) languages.

INPUT:
  Word A: <word_A> (language: <langA>, power: <powerA>)
  Word B: <word_B> (language: <langB>, power: <powerB>)

OUTPUT (single JSON, no preamble):
{
  "compatibility": <float 0-1>,
  "suggested_word": "<new word>",
  "suggested_language": "<lang>",
  "rationale": "<one sentence>"
}
```

---

## 4. Sybil Resistance — KYA + Mining Bond

### 4.1 The threat

Without economic + identity gating, scripts spawn 10,000 fresh AWP wallets, each registers as a separate agent, each mints 3, attacker walks with 30,000 Ardinals. The whole "intelligence required" narrative collapses.

### 4.2 Two-layer defense

**Layer 1 — KYA attestation** (identity)
- Must hold a valid KYA attestation to register as Ardi miner
- KYA itself enforces 1 entity = 1 attestation (via biometric + social + economic checks per AIP-3)
- Ardi calls `KYA.is_verified_agent(address)` at registration time

**Layer 2 — Mining Bond** (capital)
- Agent locks **10,000 $AWP** at registration
- Bond returned in full when:
  - Agent has minted 3 Ardinals (cap reached), OR
  - Mining period sealed + 24h cooldown elapsed
- Bond slashed (50%-100%) if KYA flags sybil collusion

### 4.3 Bond economics

At $AWP ≈ $0.001 (current rough mark), 10,000 $AWP ≈ **$10 per agent**. Light enough for legit retail, real enough that sybils take real loss:

| Sybil scale | Capital lock-up | Lock duration | Slash risk |
|---:|---:|---:|---|
| 100 fake agents | $1,000 | 7-10 days | low (KYA may miss) |
| 1,000 | $10,000 | 7-10 days | moderate (KYA pattern detect) |
| 10,000 | **$100,000** | 7-10 days | high (KYA almost certain to flag) |
| 100,000 | $1,000,000 | 7-10 days | near-certain detection |

Combined with KYA's biometric/social/economic gating, the cost-benefit becomes unfavorable for serious sybil attempts.

### 4.4 Slash flow

Slashed $AWP routes:
- 50% → burned
- 50% → fusion reward pool (rewards honest forgers)

Routing rationale: half deflates $AWP, half rewards the honest Ardi participants who proved their worth.

### 4.5 Registration flow

```
Agent
  │
  ├─ Verify on KYA (existing AWP-wide identity layer)
  │
  ├─ Approve AWP transfer (10,000 $AWP)
  │
  └─ Call Ardi.registerMiner(kya_proof_hash)
        │
        ├─ Contract verifies KYA attestation
        ├─ Contract pulls 10,000 $AWP into bond escrow
        ├─ Contract marks isMiner[address] = true
        └─ Emit MinerRegistered event
```

### 4.6 Updated sybil resistance summary

| Layer | Mechanism |
|---|---|
| **KYA** | Verified agent identity (biometric + social + economic) |
| **Mining Bond** | 10,000 $AWP lock per registration |
| **Riddle solving** | Must answer correctly (intelligence) |
| **Random draw** | 1 winner per riddle (no compute advantage) |
| **Per-epoch limit** | 5 submissions max per agent |
| **Lifetime cap** | 3 Ardinals max per agent |

---

## 5. Token — `$ardi`

### 5.1 Specs

| Parameter | Value |
|---|---|
| Name | ardi |
| Symbol | $ardi |
| Standard | ERC-20 |
| Chain | Base |
| Total supply (cap) | 10,000,000,000 |
| Initial LP | 1,000,000,000 $ardi + 1,000,000 $AWP, locked permanently |
| Pair | $ardi / $AWP on Uniswap V4 |
| Minter | WorkNet contract (sole minter) |
| Pre-mine / team allocation | none |

### 5.2 Two-phase emission (9B over 180 days)

#### Phase 1 — Mining Rush (days 1-14, 70% = 6.3B)

5-day half-life:
```
emission(d) = 954M × 0.8706^(d-1)    for d ∈ [1, 14]
```

| Day | Daily | Cumulative | % |
|---:|---:|---:|---:|
| 1 | 954M | 954M | 11% |
| 3 | 723M | 2.55B | 28% |
| 5 | 477M | 4.04B | 45% |
| 7 | 362M | 5.05B | 56% |
| 10 | 239M | 6.05B | 67% |
| 14 | 139M | **6.30B** | **70%** |

#### Phase 2 — Long Tail (days 15-180, 30% = 2.7B)

30-day half-life:
```
emission(d) = 63M × 0.9772^(d-15)    for d ∈ [15, 180]
```

| Day | Daily | Cumulative | % |
|---:|---:|---:|---:|
| 15 | 63M | 6.36B | 71% |
| 30 | 45M | 7.18B | 80% |
| 60 | 22M | 8.16B | 91% |
| 90 | 11M | 8.65B | 96% |
| 180 | <1M | **9.00B** | **100%** |

Curve shape: chip front-loaded for mining + early forge era, then 6-month tail for long-term holders. Total 180 days, 99% within 120 days.

### 5.3 Daily distribution — dual stream, single Merkle

Holders earn **two tokens** in one daily Merkle airdrop:

| Stream | Share | Source |
|---|---:|---|
| **$aArdi emission** (worknet token) | **100% to holders** | Daily on-chain mint per the 180-day curve (§5.2) |
| **AWP receipt** (from RootNet's DAO-voted share) | **`ownerOpsBps` to operator (10% default)** + remainder to holders | Pushed daily by the AWP protocol to `ArdiMintController` |

- Holder share is **power-weighted**: a holder with Power P out of total active Power gets `(P / total_active_P) × stream_pool` for each stream.
- The Merkle leaf format is `keccak256(abi.encodePacked(account, ardiAmount, awpAmount))`, so a single `claim(day, ardi, awp, proof)` call disburses both.
- `ownerOpsBps` is a Timelock-set basis-point share, **hard-capped at 2000 (20%)** in `ArdiMintController.MAX_OWNER_OPS_BPS`. The default is 1000 (10%). The cap cannot be raised even by Timelock.
- Operator AWP withdrawals always route to the configured `ownerOpsAddr` regardless of caller — a hot-key compromise on the operator role cannot redirect funds.

No team allocation on $aArdi. No hidden channel. The on-chain
ArdiMintController is the only contract authorized to mint $aArdi (after
the one-shot LP mint), the schedule is deterministic, and both reward
streams are auditable per-day on chain.

### 5.3.1 AWP ops cut — what it pays for, and why it's bounded

The 10% (or whatever `ownerOpsBps` is set to) flowing from AWP receipts
to the operator funds three things:
- **Coordinator hosting + RPC + Chainlink VRF subscription** — required
  for the protocol to make daily forward progress.
- **External audits + bug bounty** — recurring cost, paid in AWP from
  the same stream.
- **Vault rotations + key custody** — HSM/KMS subscription, secure
  backups, on-call response.

Because the AWP value of a WorkNet is itself a function of how
useful + active the WorkNet is (per §0 AWP framework: AWP price tracks
RootNet activity), the operator's incentive scales with the protocol's
health. There is no preferential allocation from the worknet token's
own emission — the operator's revenue is purely competitive AWP.

### 5.4 Slash redistribution (separate stream)

Slashed Mining Bonds flow as in §4.4 — separate from emission, additive.

---

## 6. Architecture

### 6.1 System diagram

```
┌──────────────┐        ┌────────────────┐
│   AI Agent   │───────▶│   Coordinator  │ (centralized service, Owner-run)
└──────────────┘        └────────┬───────┘
        │                        │
        │ KYA proof              │ publishes riddles, verifies submissions,
        │ + 10K AWP              │ runs random draw, signs mint authorizations,
        │                        │ invokes LLM oracle for fusions
        ▼                        ▼
┌──────────────────────────────────────────┐
│         Ardi Smart Contracts (Base)       │
│  ┌────────────┐  ┌──────────┐  ┌──────┐  │
│  │ ARDI-NFT   │  │ ARDI-FT  │  │ BOND │  │
│  │ (ERC-721)  │  │ (ERC-20) │  │ ESC  │  │
│  └────────────┘  └──────────┘  └──────┘  │
│  ┌────────────┐  ┌──────────┐            │
│  │   FORGE    │  │   OTC    │            │
│  └────────────┘  └──────────┘            │
└──────────────────────────────────────────┘
        ▲                        ▲
        │                        │
        │ stake interface        │ identity check
        │                        │
┌──────────────┐        ┌────────────────┐
│ AWP Token /  │        │  KYA WorkNet   │
│  RootNet     │        │  (AIP-3)       │
└──────────────┘        └────────────────┘
```

### 6.2 Smart contracts (5 modules on Base)

#### 6.2.1 `ArdiNFT` (ERC-721)

```solidity
struct Inscription {
    string  word;            // revealed only on mint
    uint256 power;
    address inscriber;
    uint256 timestamp;
    uint256 generation;      // 0 = original, 1+ = fusion product
    uint256[] parents;       // length 0 (original) or 2 (fusion)
    uint8   languageId;      // 0=en, 1=zh, 2=ja, 3=ko, 4=fr, 5=de
}

mapping(uint256 => Inscription) inscriptions;   // tokenId → struct
mapping(uint256 => bool) public wordMinted;     // wordId 0..20999
mapping(address => uint8) public agentMintCount;
uint256 public totalInscribed;                  // 0 .. 21000
uint256 public fusionCount;
bool    public sealed;

function inscribe(
    uint256 wordId,
    string  calldata word,
    uint256 power,
    uint8   languageId,
    bytes   calldata coordinatorSig
) external;

function fuse(
    uint256 tokenIdA,
    uint256 tokenIdB,
    string  calldata newWord,
    uint256 newPower,
    uint8   newLangId,
    bool    success,
    bytes   calldata coordinatorSig
) external;
```

#### 6.2.2 `ArdiToken` (ERC-20, $ardi)

Standard ERC-20 with controlled minter. `mint(address, uint256)` callable only by `ArdiMintController`. Hard cap 10B enforced.

#### 6.2.3 `ArdiMintController` (settlement)

- Daily settlement at UTC 00:00
- Pulls emission per the two-phase formula
- Splits 90% holder / 10% fusion pool
- Builds Merkle tree of holder claims
- Submits root, allows holders to claim with proof

#### 6.2.4 `ArdiBondEscrow`

```solidity
mapping(address => uint256) public bondAmount;
mapping(address => uint256) public bondLockUntil;

function registerMiner(bytes32 kyaProof) external {
    require(KYA.isVerified(msg.sender, kyaProof), "NO_KYA");
    require(bondAmount[msg.sender] == 0, "ALREADY_BONDED");
    AWP.transferFrom(msg.sender, address(this), 10_000 ether);
    bondAmount[msg.sender] = 10_000 ether;
    bondLockUntil[msg.sender] = type(uint256).max; // unlocked by event
    isMiner[msg.sender] = true;
}

function unlockBond() external {
    require(canUnlock(msg.sender), "STILL_LOCKED");
    uint256 amt = bondAmount[msg.sender];
    bondAmount[msg.sender] = 0;
    AWP.transfer(msg.sender, amt);
}

function slashBond(address agent, uint16 bps) external onlyKYA {
    // KYA-detected sybil → slash 5000-10000 bps (50-100%)
    uint256 slashed = bondAmount[agent] * bps / 10_000;
    bondAmount[agent] -= slashed;
    // 50% burn, 50% to fusion pool
    AWP.burn(slashed / 2);
    fusionPool.deposit(slashed / 2);
}
```

#### 6.2.5 `ArdiForge`

Wraps the LLM oracle interaction. Core fusion logic from §3. Holds reference to Coordinator's signing key.

#### 6.2.6 `ArdiOTC`

```solidity
function list(uint256 tokenId, uint256 priceWei) external;
function unlist(uint256 tokenId) external;
function buy(uint256 tokenId) external payable;     // 100% to seller, 0 protocol fee
```

### 6.3 Coordinator service (off-chain, Owner-operated)

Components:

| Service | Responsibility |
|---|---|
| **Vault Server** | Stores 21,000 `(word, riddle, power, language, merkle_proof)`. Reveals `word` only at mint signing. |
| **Epoch Engine** | Drives 3-min loop. Selects 15 riddles per epoch by rarity/lang weights. Publishes riddle list. |
| **Submission Receiver** | Accepts encrypted agent submissions. Decrypts only after epoch close. |
| **Match & Draw** | For each riddle, filter to correct guesses, run on-chain seed for random pick. |
| **Mint Signer** | Builds `(wordId, word, power, langId, address)` signature for ArdiNFT.inscribe. |
| **Fusion Oracle** | Receives fuse() request. Calls LLM. Caches result. Signs back to ArdiForge.fuse. |
| **Settlement Worker** | Daily: snapshot holdings, compute Power weights, build Merkle, submit root. |
| **KYA Bridge** | Reads KYA attestations; subscribes to KYA's sybil-detection events; triggers slashBond when needed. |

### 6.4 Agent skill (`@ardiworknet/ardi-skill`)

Distributed via [awp-worknet/ardi-skill](https://github.com/awp-worknet/ardi-skill) repo. Installable into any Claude Code session:

```bash
mkdir -p ~/.claude/skills/ardi
curl -sL https://raw.githubusercontent.com/awp-worknet/ardi-skill/main/SKILL.md \
  -o ~/.claude/skills/ardi/SKILL.md
```

Skill capabilities:
- Onboard: KYA verification + bond approval
- Subscribe: receive each epoch's 15 riddles
- Solve: LLM reasoning (user picks model — Sonnet/Opus/GPT/Gemini/etc.)
- Submit: up to 5 ranked guesses per epoch
- Track: own mint count, bond status, $ardi accruals
- Forge: list owned Ardinals, propose fusions, execute

---

## 7. Implementation Roadmap

### Phase 0 — Pre-launch (T-30 → T-0)

| Task | Owner | Status |
|---|---|---|
| Vault generation + curation | Owner team | ✅ done (`wordbank-builder/riddles.json`) |
| Vault Merkle tree | Owner team | ⏳ |
| Smart contracts written | Solidity dev | ⏳ |
| Contract audit | external auditor | ⏳ |
| Coordinator service implementation | backend team | ⏳ |
| KYA integration test (against KYA testnet) | Owner team | ⏳ |
| Agent skill (`@ardiworknet/ardi-skill`) v1 | Owner team | ⏳ |
| Frontend (mint dashboard, fusion UI, leaderboard) | Owner team | ⏳ (out of this spec) |
| Docs site | content team | ⏳ |
| Liquidity prep (1B $ardi + 1M $AWP escrow) | Owner / AWP protocol | ⏳ |

### Phase 1 — Launch day (T = 0)

1. Deploy `ArdiToken`, `ArdiNFT`, `ArdiBondEscrow`, `ArdiForge`, `ArdiOTC`, `ArdiMintController` in dependency order
2. Write Vault Merkle root to `ArdiNFT`
3. AWP RootNet auto-mints 1B $ardi → `ArdiToken` → permanently locked Uniswap V4 LP with 1M $AWP
4. Coordinator goes live, publishes first epoch's 15 riddles
5. Mining begins

### Phase 2 — Mining (T+0 → T+~7 days)

- Continuous 3-min epochs
- 21,000 Ardinals minted across (estimated) 5-10 days at ~60% solve rate
- $ardi emission accrues daily per Phase 1 schedule
- Holders can fuse in parallel
- Coordinator monitors KYA for sybil flags

### Phase 3 — Sealed era (T+~7 days → forever)

- `totalInscribed >= 21000` triggers `_seal()`
- No further originals
- Fusion continues indefinitely (only deflation)
- $ardi emission continues 180 days total
- OTC market drives price discovery
- Forge competition becomes the primary on-chain activity

---

## 8. Operational policies

### 8.1 Stuck riddles — graduated hints (no word swap)

The vault is fixed. No word ever gets removed or replaced. If a riddle stays unsolved, Coordinator escalates hints:

| Threshold | Action |
|---|---|
| 50 epochs (~2.5 hours) | Publish one additional hint sentence appended to the riddle |
| 100 epochs (~5 hours) | Publish a second, more explicit hint |
| 200 epochs (~10 hours) | Publish the word's language + character/letter count |

Hints are appended to the original riddle text, never replacements — agents still need to reason from the clues.

**Stuck-riddle fallback**: at the third hint level the riddle includes the
target language plus character/letter count, which together with the
contextual clues makes the answer derivable by any reasonably capable
LLM agent. Empirical testing on the 21,000-entry vault shows level-3
hints push solve rates above 90% within a small number of additional
epochs. If a single riddle remains genuinely unsolvable across the
post-hint window, mining proceeds without it — the protocol does not
intervene to swap or substitute words. Vault immutability is preserved
unconditionally.

### 8.2 Coordinator outage

- If Coordinator down for > 30 min during mining: epoch counter pauses; agents see status
- All in-flight mints / fusions resume on recovery
- Hard limit: 24h cumulative outage triggers DAO review

### 8.3 LLM oracle versioning

- Pin model + version (`claude-sonnet-4.6` or whatever) at launch
- Migration to newer model requires DAO vote (governance hook to GOV WorkNet, AIP-5)
- Cache results forever — never re-query a (wordA, wordB) pair

---

## 9. Security considerations

### 9.1 Vault leak

- Risk: if 21,000 (id, word) pairs leak, agents can hash-lookup answers without LLM
- Mitigation: vault stored only on Coordinator; only `(id, riddle, power, language)` revealed
- Detection: word answers in submissions form a fingerprint; identical-pattern submissions across many agents → leak suspected → KYA escalation

### 9.2 Random draw manipulation

- Seed = `keccak256(blockhash, epoch_id, riddle_id, sorted(correct_agents))`
- Coordinator can't predict future blockhash
- Sorting agents canonically prevents inclusion-based manipulation
- Verifiable: anyone can recompute and check

### 9.3 LLM oracle manipulation

- Temperature=0 → deterministic
- Cache → repeats free, no re-query attack
- Mitigation roadmap: future AIP for multi-oracle / verifiable inference

### 9.4 Front-running submissions

- Submissions encrypted in transit (X25519 to Coordinator's epoch key)
- Decrypted only after epoch close
- Coordinator MUST commit to encrypted submission set before revealing

### 9.5 Concentrated Power via fusion

- By design: fusion concentrates Power
- Whales earn higher airdrop share, but burn Ardinals to do so
- Net: scarcity rises, supply shrinks, individual whales pay (in burnt NFTs) for their Power consolidation
- Not a bug

### 9.6 Bond griefing

- Attacker registers, never submits, ties up own capital for 7-10 days
- Lost opportunity cost only — doesn't harm protocol
- No mitigation needed (self-punishing)

### 9.7 KYA-bypass

- Must hold valid KYA attestation to register
- Cost of fake KYA attestation = full KYA cost (designed to be high)
- Stack: KYA cost + 10K $AWP bond + 7-10 day lock = sybil unprofitable

---

## 10. Open questions

| # | Question | Decision |
|---|---|---|
| 1 | Cross-language fusion output language | **LLM picks randomly** between either parent's language (§3.4) |
| 2 | LLM model pinning | **None** — any LLM acceptable (no version lock) |
| 3 | Stuck riddle hint policy | **Graduated hints at 50/100/200 epochs only — no word swap.** Vault is immutable; if a riddle remains unsolved the slot is skipped (§8.1) |
| 4 | DAO transition | **Deferred** — revisit post-mint (after seal) |
| 5 | Cross-chain plans | **Out of scope** — Ardi stays on Base only |
| 6 | Frontend / brand / URL | **Owner team handles** — out of this spec's scope |

---

## 11. Pre-launch checklist

- [ ] Vault Merkle root finalized
- [ ] All 5 contracts audited
- [ ] Coordinator service load-tested (3-min epoch under 10K concurrent agents)
- [ ] KYA testnet integration verified
- [ ] Agent skill published, doc complete
- [ ] LP escrow ready (1B $ardi + 1M $AWP)
- [ ] Frontend deployed
- [ ] Documentation site live
- [ ] Launch announcement scheduled
- [ ] Community ops ready (forum, discord, agent onboarding flow)

---

## 12. Parameter quick-reference card

| Category | Parameter | Value |
|---|---|---|
| Vault | Total | 21,000 |
| Vault | Languages | en/zh/ja/ko/fr/de (6,000/5,000/3,000/3,000/2,000/2,000) |
| Vault | Power range | 1-100 |
| Vault | Rarity thresholds | 74+/62-73/52-61/<52 → leg/rare/uncom/com |
| Mining | Epoch duration | 3 min |
| Mining | Riddles per epoch | 15 |
| Mining | Submissions per agent per epoch | up to 5 |
| Mining | Lifetime mints per agent | 3 |
| Sybil | KYA required | yes |
| Sybil | Mining Bond | 10,000 $AWP |
| Sybil | Bond lock | mining period + 24h |
| Sybil | Slash | 50%-100% on KYA flag |
| Forge | Cooldown | 24h per address |
| Forge | LLM | Claude (TBD), temperature=0 |
| Token | Total supply | 10,000,000,000 $ardi |
| Token | Initial LP | 1B $ardi + 1M $AWP locked |
| Token | Phase 1 emission | 70% over 14 days, 5-day half-life |
| Token | Phase 2 emission | 30% over 166 days, 30-day half-life |
| Token | Distribution | 90% holders / 10% fusion pool |

---

*End of design spec. Companion files: `aip-003-v3.md` (governance proposal), `wordbank-builder/riddles.json` (vault).*
