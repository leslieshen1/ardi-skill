// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Ownable2Step} from "@openzeppelin/contracts/access/Ownable2Step.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {IKYA} from "./interfaces/IKYA.sol";
import {IArdiBondEscrow} from "./interfaces/IArdiBondEscrow.sol";

interface IBurnableToken {
    function burn(uint256 amount) external;
}

/// @notice Minimal view of ArdiEpochDraw used by BondEscrow to read agents'
///         resolved win count. Decouples this contract from the full draw
///         interface; only the per-agent win counter is required here.
interface IEpochDrawWinView {
    function agentWinCount(address agent) external view returns (uint8);
    function MAX_WINS_PER_AGENT() external view returns (uint8);
}

/// @title ArdiBondEscrow — KYA-gated registration + 10K $AWP Mining Bond
/// @notice Agents must (a) hold a valid KYA attestation and (b) lock 10,000 $AWP
///         in this escrow to be eligible to mint Ardinals. The bond is fully
///         refundable on graceful exit; slashed on KYA-confirmed sybil.
/// @dev    Lifecycle:
///           registerMiner()  → bond locked, isMiner=true
///           ArdiNFT.inscribe() runs, calling onMinted(agent) here as a hook
///           When agent has minted MAX (3) OR sealed period elapsed → unlock
///           If KYA flags sybil cluster anytime before unlock → slashOnSybil()
contract ArdiBondEscrow is IArdiBondEscrow, Ownable2Step, ReentrancyGuard {
    using SafeERC20 for IERC20;

    // --- Constants ---

    uint256 public constant BOND_AMOUNT = 10_000 ether; // 10,000 $AWP (assumes 18 decimals)
    /// @notice Kept for ABI stability (indexers / UIs read this). Actual
    ///         enforcement is via the IEpochDrawWinView.MAX_WINS_PER_AGENT
    ///         constant pulled at unlock-bond time. Both should be 3.
    uint8 public constant MAX_MINTS_PER_AGENT = 3;
    uint256 public constant POST_SEAL_COOLDOWN = 24 hours;

    // --- External contracts ---

    IERC20 public immutable AWP;
    IKYA public KYA;
    address public ardiNFT; // ArdiNFT contract address (still used by setSealed)
    /// @notice Source-of-truth for win count gating bond unlock. Set by owner
    ///         after deploy. v1.0+: replaces the per-mint counter that used to
    ///         live here. Reads `agentWinCount(agent)` and treats `>= MAX`
    ///         as "cap reached" for unlock purposes.
    IEpochDrawWinView public epochDraw;

    /// @notice Destination for the 50% slash share of forfeited Mining Bonds.
    /// @dev    Historically called "fusionPool"; with the live tokenomics
    ///         (100% holder split on $ardi emission) this address now functions
    ///         purely as the protocol Treasury for slash proceeds. Must be a
    ///         multisig / treasury contract — pointing it at the escrow itself
    ///         locks tokens permanently because BondEscrow has no withdraw path.
    ///         Renamed in Deploy.s.sol intent (TREASURY_ADDR); kept the on-chain
    ///         name `fusionPool` for ABI stability.
    address public fusionPool;

    // --- Per-agent state ---

    struct MinerState {
        uint128 bondAmount;
        uint64 registeredAt;
        /// @dev v1.0: legacy field, no longer written to. Kept in storage for
        ///      ABI compatibility with off-chain code that decodes the
        ///      `miners` mapping. Always 0 going forward — `isMiner` and
        ///      `unlockBond` now read win count from EpochDraw.
        uint8 mintCount;
        bool isActive;
        bool slashed;
    }

    mapping(address => MinerState) public miners;

    /// @notice Set by ArdiNFT once 21,000 originals minted (sealed). Drives
    ///         the post-seal cooldown that releases all remaining bonds.
    uint256 public sealedAt;

    // --- Events ---

    event MinerRegistered(address indexed agent, uint256 bondAmount);
    event BondUnlocked(address indexed agent, uint256 amount);
    event BondSlashed(address indexed agent, uint256 burned, uint256 toFusionPool);
    event MintRecorded(address indexed agent, uint8 newCount);
    event Sealed(uint256 sealedAt);

    event KYASet(address indexed kya);
    event ArdiNFTSet(address indexed nft);
    event FusionPoolSet(address indexed pool);
    event EpochDrawSet(address indexed epochDraw);

    // --- Errors ---

    error NotKYAVerified();
    error AlreadyRegistered();
    error NotRegistered();
    error StillLocked();
    error AlreadySlashed();
    error NotArdiNFT();
    error NotKYAOrOwner();
    error NotSybil();
    error InvalidBps();
    error ZeroAddress();
    error EpochDrawNotSet();

    // --- Constructor ---

    constructor(address initialOwner, address awp_, address kya_, address fusionPool_)
        Ownable(initialOwner)
    {
        if (awp_ == address(0) || kya_ == address(0) || fusionPool_ == address(0)) revert ZeroAddress();
        AWP = IERC20(awp_);
        KYA = IKYA(kya_);
        fusionPool = fusionPool_;
    }

    // --- Admin ---

    function setKYA(address kya_) external onlyOwner {
        if (kya_ == address(0)) revert ZeroAddress();
        KYA = IKYA(kya_);
        emit KYASet(kya_);
    }

    function setArdiNFT(address nft_) external onlyOwner {
        if (nft_ == address(0)) revert ZeroAddress();
        ardiNFT = nft_;
        emit ArdiNFTSet(nft_);
    }

    /// @notice Wire up the source-of-truth for per-agent win counts. Must
    ///         be called once after deploy before any unlockBond can succeed.
    function setEpochDraw(address epochDraw_) external onlyOwner {
        if (epochDraw_ == address(0)) revert ZeroAddress();
        epochDraw = IEpochDrawWinView(epochDraw_);
        emit EpochDrawSet(epochDraw_);
    }

    function setFusionPool(address pool_) external onlyOwner {
        if (pool_ == address(0)) revert ZeroAddress();
        // Block re-introduction of the original C-2 footgun at runtime: pointing
        // fusionPool at this contract locks any tokens routed here forever
        // (BondEscrow has no $ardi or AWP withdraw path).
        if (pool_ == address(this)) revert ZeroAddress();
        fusionPool = pool_;
        emit FusionPoolSet(pool_);
    }

    /// @notice Mark mining as sealed. Called by ArdiNFT when the 21,000-th
    ///         original is minted. Permissioned to ArdiNFT only — the prior
    ///         owner-callable branch was a backdoor that let owner refund all
    ///         bonded miners post-cooldown without any mint requirement.
    function setSealed() external {
        if (msg.sender != ardiNFT) revert NotArdiNFT();
        if (sealedAt == 0) {
            sealedAt = block.timestamp;
            emit Sealed(block.timestamp);
        }
    }

    // --- Register / unregister ---

    /// @notice Become a miner. Requires KYA verification + 10K $AWP approval.
    /// @dev    Pulls bond from caller. Caller must have approved this contract
    ///         to transfer at least BOND_AMOUNT $AWP beforehand.
    function registerMiner() external nonReentrant {
        if (!KYA.isVerified(msg.sender)) revert NotKYAVerified();
        if (miners[msg.sender].isActive) revert AlreadyRegistered();

        AWP.safeTransferFrom(msg.sender, address(this), BOND_AMOUNT);

        miners[msg.sender] = MinerState({
            bondAmount: uint128(BOND_AMOUNT),
            registeredAt: uint64(block.timestamp),
            mintCount: 0,
            isActive: true,
            slashed: false
        });

        emit MinerRegistered(msg.sender, BOND_AMOUNT);
    }

    /// @notice v1.0: legacy hook, kept as a no-op for ABI compatibility.
    ///         Mint counts are no longer the gating signal — ArdiEpochDraw's
    ///         agentWinCount is. ArdiNFT.inscribe no longer calls this.
    function onMinted(address agent) external override {
        if (msg.sender != ardiNFT) revert NotArdiNFT();
        // Intentionally empty. Old behavior would `++m.mintCount` here; the
        // counter is now ignored by isMiner / unlockBond.
        agent; // silence unused-arg warning
    }

    /// @notice Withdraw bond. Conditions:
    ///         (a) agent's winCount has reached MAX_WINS_PER_AGENT (cap), OR
    ///         (b) sealed && now > sealedAt + POST_SEAL_COOLDOWN
    /// @dev    v1.0: cap is read from ArdiEpochDraw.agentWinCount(agent).
    ///         A win consumes a slot whether or not the winner inscribed —
    ///         this is the natural punishment for "win and squat" agents.
    function unlockBond() external nonReentrant {
        MinerState storage m = miners[msg.sender];
        if (!m.isActive) revert NotRegistered();
        if (m.slashed) revert AlreadySlashed();
        if (address(epochDraw) == address(0)) revert EpochDrawNotSet();

        uint8 wins = epochDraw.agentWinCount(msg.sender);
        uint8 cap = epochDraw.MAX_WINS_PER_AGENT();
        bool capReached = wins >= cap;
        bool postSealCooldown = sealedAt != 0 && block.timestamp >= sealedAt + POST_SEAL_COOLDOWN;
        if (!capReached && !postSealCooldown) revert StillLocked();

        uint256 amount = m.bondAmount;
        m.bondAmount = 0;
        m.isActive = false;

        AWP.safeTransfer(msg.sender, amount);
        emit BondUnlocked(msg.sender, amount);
    }

    // --- Slashing ---

    /// @notice Slash an agent's bond. Callable by KYA contract or owner.
    ///         50% to burn, 50% to fusion pool by default.
    /// @param  agent Agent to slash
    /// @param  bps   Basis points to slash (10_000 = 100%)
    function slashOnSybil(address agent, uint16 bps) external nonReentrant {
        if (msg.sender != address(KYA) && msg.sender != owner()) revert NotKYAOrOwner();
        if (bps == 0 || bps > 10_000) revert InvalidBps();
        if (!KYA.isSybilFlagged(agent)) revert NotSybil();

        MinerState storage m = miners[agent];
        if (!m.isActive) revert NotRegistered();
        if (m.slashed) revert AlreadySlashed();

        uint256 totalBond = uint256(m.bondAmount);
        uint256 slashAmount = (totalBond * bps) / 10_000;
        uint256 toBurn = slashAmount / 2;
        uint256 toPool = slashAmount - toBurn;
        uint256 remainder = totalBond - slashAmount;

        // Checks-Effects-Interactions: zero ALL state BEFORE any external call,
        // even though nonReentrant + trusted AWP makes this defense-in-depth.
        m.bondAmount = 0;
        m.slashed = true;
        m.isActive = false; // forfeits future minting

        if (toBurn > 0) {
            // Try burn; if AWP doesn't expose burn, fall back to dead address
            try IBurnableToken(address(AWP)).burn(toBurn) {} catch {
                AWP.safeTransfer(0x000000000000000000000000000000000000dEaD, toBurn);
            }
        }
        if (toPool > 0 && fusionPool != address(0)) {
            AWP.safeTransfer(fusionPool, toPool);
        }

        // Refund the un-slashed remainder back to agent (if any)
        if (remainder > 0) {
            AWP.safeTransfer(agent, remainder);
        }

        emit BondSlashed(agent, toBurn, toPool);
    }

    // --- Views (used by ArdiNFT) ---

    /// @notice True if the agent is a registered, non-slashed miner.
    /// @dev    v1.0 (post-fix): the win cap is enforced UPSTREAM at commit /
    ///         reveal / draw time in ArdiEpochDraw. We deliberately do NOT
    ///         re-gate on winCount here — otherwise a winner who has just
    ///         earned their 3rd win (winCount → 3 at VRF callback) would
    ///         flunk this check and be unable to inscribe their final NFT,
    ///         leaving every miner with at most 2 inscribed Ardinals. The
    ///         bond-unlock path reads agentWinCount directly; this view is
    ///         purely "are you allowed to mint a win you already earned".
    function isMiner(address agent) external view override returns (bool) {
        MinerState storage m = miners[agent];
        return m.isActive && !m.slashed;
    }
}
