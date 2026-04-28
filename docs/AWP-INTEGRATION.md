# Ardi → AWP WorkNet — Integration Plan

This document maps the Ardi protocol onto the AWP WorkNet standard, and
specifies exactly which of our existing contracts stay vs. get replaced
by AWP-framework defaults.

## Decisions (locked)

| Decision | Choice | Why |
|---|---|---|
| **Token model** | Use AWP-auto-deployed worknet token (drop our `ArdiToken.sol`) | Standard alignment; gives us auto AWP/$ardi LP pool at activation; easier Guardian approval |
| **LP model** | Auto-created at activation: 1M AWP × 1B $ardi, locked | AWP standard; permanent LP protects holders |
| **Manager contract** | Custom `ArdiMintController` keeps our 180-day emission curve + power-weighted Merkle | AWP default manager is too generic for our schedule; we adapt it to AWP's role-based interface |
| **Vault contents** | **Stays sealed/encrypted** — 21K riddles + answers never published | Riddle scarcity is the whole moat; only riddle text + power are public, the answer is hash-only |
| **AWP receipt strategy** | **10% to owner ops, 90% to holders** (power-weighted, same Merkle path as $ardi) | Operator gets maintenance budget; holders get dual reward (worknet token + AWP) |
| **$ardi emission** | **100% to NFT holders** (unchanged from current) | No owner cut on the worknet token's own emission |

## Public messaging — two distinct streams

This needs to be explicit in all public docs to avoid confusion:

| Stream | Source | Distribution |
|---|---|---|
| **$ardi emission** | Minted daily by WorkNet (180-day curve, 9B total) | **100% to NFT holders** by power. No owner cut. |
| **AWP receipt** | Received daily from AWP protocol (DAO-voted share) | **10% to operator** (ops budget) + **90% to NFT holders** by power. |

Holders thus earn BOTH $ardi AND AWP in the same Merkle airdrop. Operator
ONLY takes from the AWP stream, which is itself a function of the
WorkNet's value — it's a competitive, transparent ops budget, not a
preferential mint.

## Contract impact

### Stays (Ardi-specific, no conflict with AWP)

- `ArdiNFT.sol` — the Ardinal ERC-721 itself
- `ArdiBondEscrow.sol` — KYA + bond gating for agents
- `ArdiOTC.sol` — secondary market for Ardinals
- `ArdiEpochDraw.sol` — commit-reveal + VRF lottery
- `ChainlinkVRFAdapter.sol` — VRF source
- All `interfaces/*`

### Modified

- `ArdiMintController.sol`
  - **Add**: AccessControl (`MERKLE_ROLE`, `TRANSFER_ROLE`) per AWP manager interface
  - **Add**: AWP distribution alongside $ardi distribution in `settleDay`
  - **Add**: AWP receipt + held-balance accounting
  - **Keep**: 180-day emission curve, power-weighted Merkle airdrop, claim path
  - The Merkle leaf shape becomes `keccak256(account, ardi_amount, awp_amount)` so a single claim() call distributes both

### Dropped (replaced by AWP framework)

- `ArdiToken.sol` — replaced by the AWP-auto-deployed worknet token at
  Guardian activation. Will use the same name "Ardinal" / symbol "ardi"
  / 10B cap, but the deploy ceremony moves into AWP's hands.

### Added

- `coordinator/src/coordinator/awp_distribution.py`
  - Reads AWP balance held by the manager contract
  - Splits 10% / 90%
  - 10% → owner-ops address (operator)
  - 90% → folded into the daily Merkle alongside $ardi
- `scripts/register_worknet.py`
  - Driver for the AWP `/api/relay/register-worknet/prepare` flow
  - Outputs a worknetId once Guardian activates

## On-chain changes — concrete

### `ArdiMintController` interface (post-alignment)

```solidity
// Replaces today's settleDay(day, root, holderTotal, fusionTotal)
// with a Merkle that covers BOTH streams in one root.

function settleDay(
    uint256 day,
    bytes32 root,           // Merkle of (account, ardi_amount, awp_amount)
    uint256 ardiTotal,      // total $ardi minted to manager for this day
    uint256 awpTotalToHolders   // 90% of received AWP
) external onlyRole(MERKLE_ROLE) {
    if (dailyRoots[day].root != bytes32(0)) revert AlreadySettled();
    dailyRoots[day] = DailyRoot({...});
    ARDI_TOKEN.mint(address(this), ardiTotal);
    // AWP is already on this contract via the AWP protocol's daily push
    emit DailySettled(day, root, ardiTotal, awpTotalToHolders);
}

function claim(
    uint256 day,
    uint256 ardiAmount,
    uint256 awpAmount,
    bytes32[] calldata proof
) external nonReentrant {
    if (claimed[day][msg.sender]) revert AlreadyClaimed();
    bytes32 leaf = keccak256(abi.encodePacked(msg.sender, ardiAmount, awpAmount));
    if (!MerkleProof.verify(proof, dailyRoots[day].root, leaf)) revert InvalidProof();
    claimed[day][msg.sender] = true;

    if (ardiAmount > 0) ARDI_TOKEN.transfer(msg.sender, ardiAmount);
    if (awpAmount > 0)  AWP.safeTransfer(msg.sender, awpAmount);
    emit Claimed(day, msg.sender, ardiAmount, awpAmount);
}

// Operator pulls their 10% AWP cut periodically. Same role as Merkle role
// but a separate accounting bucket.
function withdrawOwnerAwp(uint256 amount) external onlyRole(OWNER_OPS_ROLE) {
    require(amount <= ownerAwpReserve, "exceeds reserve");
    ownerAwpReserve -= amount;
    AWP.safeTransfer(msg.sender, amount);
    emit OwnerAwpWithdrawn(msg.sender, amount);
}
```

The Coordinator's settlement_worker.py owns the off-chain math:
- Compute daily $ardi emission per the 180-day curve
- Read AWP received today by the manager
- Split AWP: 10% → ownerAwpReserve, 90% → distribute pool
- Snapshot holders + power
- Build Merkle of (addr, ardi_share, awp_share)
- Call settleDay(day, root, ardiTotal, awpTotalToHolders)

### Bond escrow stays unchanged

Agents still register with 10K $AWP via `ArdiBondEscrow.registerMiner()`.
This is a SEPARATE balance from the WorkNet's daily AWP receipts — the
former is participant collateral, the latter is reward.

## Registration plan (sequence)

1. **Materials prep** (1-2 days)
   - `scoring.md` published — explains Power, riddle solving, fusion oracle
   - All contracts verified on Base
   - `skillsURI` pointed at `agent-skill/` GitHub
   - 1M $AWP in the team's deploying wallet
2. **`registerWorkNet` call** (5 min)
   - `POST /api/relay/register-worknet/prepare` — get permitTypedData + registerTypedData
   - Sign both with deployer wallet
   - `POST /api/relay/register-worknet` — get tx + worknetId
   - Status: Pending
3. **Guardian submission** (calendar 3-7 days)
   - Email `hi@agentmail.to` with worknetId + materials
   - Guardian (3-of-5) reviews:
     - Genuine AI utility ✓ (riddle solving requires LLM reasoning)
     - Security + auditability ✓ (slither clean, internal audits done, external audit in progress)
     - Openness + fairness ✓ (see design-spec §0.5 Provable Fairness)
4. **Activation** (single atomic tx)
   - AWP deploys our worknet token (name=Ardinal, symbol=ardi, cap=10B)
   - LP pool auto-seeded: 1M AWP × 1B ardi, locked
   - AWP manager contract initialized — but we DON'T use it; we point the
     WorkNet's manager pointer at our `ArdiMintController` (post-aligned)
   - WorkNet NFT minted to operator
5. **Operator config** (15 min)
   - `grantRole(MERKLE_ROLE, coordinatorOpsKey)`
   - `grantRole(TRANSFER_ROLE, coordinatorOpsKey)`
   - Set AWP strategy: 10% reserve / 90% to holders (via Coordinator config)
6. **First settle** (T+24h)
   - Coordinator runs `settleDay(1, ...)` with the new combined Merkle
   - Holders can claim both $ardi and AWP

## What needs to happen in our code

| Task | File(s) | Effort |
|---|---|---|
| Add AccessControl roles to ArdiMintController | `contracts/src/ArdiMintController.sol` | 2 hours |
| Update settleDay + claim signature for dual-token Merkle | same | 2 hours |
| Add AWP receipt accounting (`ownerAwpReserve` etc.) | same | 1 hour |
| Update Coordinator settlement to compute combined Merkle | `coordinator/src/coordinator/settlement.py` | 2 hours |
| Add `awp_distribution_worker.py` for the 10/90 split | new file | 2 hours |
| Update tests (forge + pytest) | `contracts/test/`, `coordinator/tests/` | 4 hours |
| Update tokenomics docs to mention dual stream | `docs/aip-003-v3.md`, `docs/design-spec.md` | 30 min |
| Drop `ArdiToken.sol` + tests + deploy script ref | clean removal | 1 hour |
| `scripts/register_worknet.py` driver | new | 2 hours |
| Update SKILL.md to mention dual reward | `agent-skill/SKILL.md` | 15 min |

**Total: ~17 hours** of focused engineering.

## Sequencing recommendation

Do contracts + tests first (T1-T6 above), since those are the structural
changes Guardian will inspect. Then:
- Have me commit + run all tests + e2e
- Wait for your review (you may want to verify the 10/90 split semantics)
- Then update docs + register_worknet.py
- Then submit registration

## Open questions (for you to confirm)

1. **owner-ops AWP withdrawal cadence** — withdraw every settlement?
   weekly? monthly? Affects cash-flow planning.
2. **ownerOpsAddress** — single EOA, multisig, Timelock-controlled? I'd
   recommend the same Treasury multisig that holds slash AWP (audit C-2
   alignment).
3. **AWP strategy mix flexibility** — start at fixed 10/90, or build it
   so the Timelock can adjust the split (e.g. drop ops % to 5% later)?
   Recommend Timelock-adjustable.
4. **Worknet token name + symbol** — keep "Ardinal" / "ardi"? These are
   permanent post-Guardian.
