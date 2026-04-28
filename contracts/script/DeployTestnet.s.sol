// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Script, console2} from "forge-std/Script.sol";
import {ArdiToken} from "../src/ArdiToken.sol";
import {ArdiNFT} from "../src/ArdiNFT.sol";
import {ArdiBondEscrow} from "../src/ArdiBondEscrow.sol";
import {ArdiOTC} from "../src/ArdiOTC.sol";
import {ArdiMintController} from "../src/ArdiMintController.sol";
import {ArdiEpochDraw} from "../src/ArdiEpochDraw.sol";
import {MockRandomness} from "../src/MockRandomness.sol";
import {MockAWP, MockKYA} from "../test/Mocks.sol";

/// @title Testnet deployment — Base Sepolia (chainId 84532)
/// @notice AWP isn't on testnet yet, so we deploy MockAWP / MockKYA / MockRandomness
///         alongside the real contracts. This is the "Ardi WorkNet pre-AWP-activation
///         rehearsal" — same contract logic + Merkle + claim flow as mainnet, but
///         without going through AWP Guardian registration.
///
/// Required env (loaded via `source .testnet/deployer.env` before running):
///   DEPLOYER_PK        — broadcasts deploy txs
///   COORDINATOR_PK     — Coordinator service signing key (settleDay, fusion auth)
///                        For testnet: same as DEPLOYER_PK (single-EOA setup)
///   TREASURY_ADDR      — slash recipient + BondEscrow.fusionPool slot
///   OWNER_OPS_ADDR     — receives the AWP ops cut from MintController
///   GENESIS_TS         — optional, defaults to block.timestamp at deploy time
///
/// Run:
///   source .testnet/deployer.env
///   cd contracts
///   forge script script/DeployTestnet.s.sol \
///     --rpc-url $BASE_SEPOLIA_RPC \
///     --broadcast \
///     --private-key $DEPLOYER_PK \
///     -v
///
/// Output: ./deployments/base-sepolia.json
contract DeployTestnet is Script {
    struct Addrs {
        address mockAWP;
        address mockKYA;
        address ardiToken;
        address ardiNFT;
        address bondEscrow;
        address otc;
        address mintController;
        address epochDraw;
        address mockRandomness;
    }

    function run() external returns (Addrs memory out) {
        uint256 deployerPk = vm.envUint("DEPLOYER_PK");
        address deployer = vm.addr(deployerPk);

        // Single-EOA testnet: coordinator = deployer unless overridden
        uint256 coordinatorPk = vm.envOr("COORDINATOR_PK", deployerPk);
        address coordinator = vm.addr(coordinatorPk);

        address owner = deployer;
        address lpEscrow = vm.envOr("LP_ESCROW_ADDR", deployer);
        address treasury = vm.envOr("TREASURY_ADDR", deployer);
        address ownerOpsAddr = vm.envOr("OWNER_OPS_ADDR", deployer);
        uint256 genesisTs = vm.envOr("GENESIS_TS", block.timestamp);

        // Sanity: only deploy on Base Sepolia (84532) by default.
        // Override via `vm.chainId` in tests if you want to reuse this script
        // on a fork.
        require(block.chainid == 84532, "DeployTestnet expects Base Sepolia (84532)");

        console2.log("Deployer        :", deployer);
        console2.log("Coordinator     :", coordinator);
        console2.log("Owner           :", owner);
        console2.log("Treasury        :", treasury);
        console2.log("OwnerOpsAddr    :", ownerOpsAddr);
        console2.log("Genesis TS      :", genesisTs);

        vm.startBroadcast(deployerPk);

        // 1. Mock AWP + Mock KYA (no real AWP/KYA on testnet)
        MockAWP awp = new MockAWP();
        MockKYA kya = new MockKYA();

        // 2. Vault Merkle root. On real launch this comes from vault_merkle.py
        //    over the 21K riddles. For testnet, we use the root computed from
        //    Regenerate via the canonical tool (v1.0 hash-only leaf format):
        //      python3 tools/vault_merkle.py \
        //        --vault data/riddles.json --out data/vault_tree_v1.json
        //    Leaf: keccak(uint256 wordId || bytes32 keccak(word) || uint16 power || uint8 lang)
        bytes32 vaultRoot =
            bytes32(uint256(0x135744267bf3b8c5cc4f998f5bac489c3cffcedfb888931e8defb0ea80a10c28));

        // 3. Core contracts
        ArdiToken token = new ArdiToken(owner);
        ArdiBondEscrow escrow = new ArdiBondEscrow(owner, address(awp), address(kya), treasury);
        ArdiNFT nft = new ArdiNFT(owner, coordinator, vaultRoot);
        ArdiOTC otc = new ArdiOTC(owner, address(nft));
        ArdiMintController ctrl = new ArdiMintController(
            owner,            // DEFAULT_ADMIN_ROLE
            address(token),
            address(awp),
            coordinator,
            ownerOpsAddr,
            genesisTs
        );

        // 3b. MockRandomness for VRF. Mainnet would use ChainlinkVRFAdapter wired
        //     to the official VRF Coordinator. Testnet stays mocked for this round
        //     so we don't need a LINK subscription.
        MockRandomness rng = new MockRandomness();
        ArdiEpochDraw epochDraw = new ArdiEpochDraw(
            owner, vaultRoot, address(rng), coordinator, treasury
        );

        // 4. Wire-up
        nft.setBondEscrow(address(escrow));
        nft.setEpochDraw(address(epochDraw));
        escrow.setArdiNFT(address(nft));
        // v1.0: BondEscrow reads agentWinCount from EpochDraw to gate bond
        // unlock. Without this wiring, unlockBond reverts EpochDrawNotSet.
        escrow.setEpochDraw(address(epochDraw));

        // 5. Initial LP mint — 1B aArdi to the deployer (LP escrow stand-in).
        //    Production: this slug is paired with 1M AWP and locked in Uniswap V4.
        //    Testnet: deployer just holds it; can manually transfer to a mock LP if needed.
        token.mintLp(lpEscrow, 1_000_000_000 ether);

        // 6. Hand emission authority to MintController, lock the minter slot
        token.setMinter(address(ctrl));
        token.lockMinter();

        // 7. Post-deploy invariants
        require(escrow.fusionPool() != address(escrow), "fusionPool == escrow (locked)");
        require(address(nft.epochDraw()) == address(epochDraw), "nft epochDraw not wired");
        require(ctrl.ownerOpsAddr() == ownerOpsAddr, "ownerOpsAddr mismatch");
        require(ctrl.coordinator() == coordinator, "coordinator mismatch");
        require(token.minter() == address(ctrl), "minter not set to ctrl");
        require(token.minterLocked(), "minter not locked");

        vm.stopBroadcast();

        out = Addrs({
            mockAWP: address(awp),
            mockKYA: address(kya),
            ardiToken: address(token),
            ardiNFT: address(nft),
            bondEscrow: address(escrow),
            otc: address(otc),
            mintController: address(ctrl),
            epochDraw: address(epochDraw),
            mockRandomness: address(rng)
        });

        console2.log("");
        console2.log("===== DEPLOYED on Base Sepolia (84532) =====");
        console2.log("MockAWP         :", out.mockAWP);
        console2.log("MockKYA         :", out.mockKYA);
        console2.log("ArdiToken       :", out.ardiToken);
        console2.log("ArdiNFT         :", out.ardiNFT);
        console2.log("ArdiBondEscrow  :", out.bondEscrow);
        console2.log("ArdiOTC         :", out.otc);
        console2.log("ArdiMintCtrl    :", out.mintController);
        console2.log("ArdiEpochDraw   :", out.epochDraw);
        console2.log("MockRandomness  :", out.mockRandomness);

        // Write addresses for Coordinator + agent skill consumption
        string memory deployJson = string.concat(
            "{\n",
            '  "chainId": ', vm.toString(block.chainid), ",\n",
            '  "network": "base-sepolia",\n',
            '  "deployedAt": ', vm.toString(block.timestamp), ",\n",
            '  "genesisTs": ', vm.toString(genesisTs), ",\n",
            '  "owner": "', vm.toString(owner), '",\n',
            '  "coordinator": "', vm.toString(coordinator), '",\n',
            '  "treasury": "', vm.toString(treasury), '",\n',
            '  "ownerOpsAddr": "', vm.toString(ownerOpsAddr), '",\n',
            '  "mockAWP": "', vm.toString(out.mockAWP), '",\n',
            '  "mockKYA": "', vm.toString(out.mockKYA), '",\n',
            '  "ardiToken": "', vm.toString(out.ardiToken), '",\n',
            '  "ardiNFT": "', vm.toString(out.ardiNFT), '",\n',
            '  "bondEscrow": "', vm.toString(out.bondEscrow), '",\n',
            '  "otc": "', vm.toString(out.otc), '",\n',
            '  "mintController": "', vm.toString(out.mintController), '",\n',
            '  "epochDraw": "', vm.toString(out.epochDraw), '",\n',
            '  "mockRandomness": "', vm.toString(out.mockRandomness), '",\n',
            '  "vaultMerkleRoot": "', vm.toString(vaultRoot), '"\n',
            "}\n"
        );
        vm.writeFile("./deployments/base-sepolia.json", deployJson);
    }
}
