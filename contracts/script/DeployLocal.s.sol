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

/// @title Local / testnet deployment script with mock AWP and KYA.
/// @notice For Anvil + Sepolia integration testing where AWP and KYA aren't deployed.
///         For mainnet, use Deploy.s.sol with real AWP / KYA addresses.
///
/// Run on Anvil:
///   anvil &
///   forge script script/DeployLocal.s.sol --rpc-url http://localhost:8545 \
///     --broadcast --private-key 0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80
contract DeployLocal is Script {
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
        uint256 deployerPk =
            vm.envOr("DEPLOYER_PK", uint256(0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80));
        address deployer = vm.addr(deployerPk);

        // Coordinator key (for signing inscribe/fuse). Public key set as `coordinator`.
        uint256 coordinatorPk =
            vm.envOr("COORDINATOR_PK", uint256(0x2222222222222222222222222222222222222222222222222222222222222222));
        address coordinator = vm.addr(coordinatorPk);

        // Use the deployer's address as initial owner (in production: multisig)
        address owner = deployer;
        address lpEscrow = vm.envOr("LP_ESCROW_ADDR", deployer);
        // For local/testnet, treasury defaults to the deployer (a real EOA),
        // so any slash / future fusion-pool tokens land somewhere recoverable.
        // On mainnet use Deploy.s.sol which requires TREASURY_ADDR explicitly.
        address treasury = vm.envOr("TREASURY_ADDR", deployer);
        // Owner-ops EOA receives the 10% AWP cut. For local default to deployer.
        address ownerOpsAddr = vm.envOr("OWNER_OPS_ADDR", deployer);
        uint256 genesisTs = vm.envOr("GENESIS_TS", block.timestamp);

        console2.log("Deployer        :", deployer);
        console2.log("Coordinator     :", coordinator);
        console2.log("Owner           :", owner);
        console2.log("Treasury        :", treasury);

        vm.startBroadcast(deployerPk);

        // 1. Mock AWP and KYA (replace with real addresses on mainnet)
        MockAWP awp = new MockAWP();
        MockKYA kya = new MockKYA();

        // 2. Mock vault Merkle root (in production: from vault_merkle.py)
        bytes32 vaultRoot =
            bytes32(uint256(0x4c52cbe743bcefd09feb473c96a0a0fc705e16c7ae320e028cd2589a71848590));

        // 3. Deploy core contracts. Treasury fills the fusionPool slot so that:
        //   - slash proceeds (50% AWP) flow somewhere recoverable instead of escrow self
        //   - if fusion_bps is ever raised above 0, the $ardi mint also goes somewhere recoverable
        ArdiToken token = new ArdiToken(owner);
        ArdiBondEscrow escrow = new ArdiBondEscrow(owner, address(awp), address(kya), treasury);
        ArdiNFT nft = new ArdiNFT(owner, coordinator, vaultRoot);
        ArdiOTC otc = new ArdiOTC(owner, address(nft));
        ArdiMintController ctrl = new ArdiMintController(
            owner,            // DEFAULT_ADMIN_ROLE (Timelock in production)
            address(token),
            address(awp),
            coordinator,
            ownerOpsAddr,
            genesisTs
        );

        // 3b. Deploy on-chain commit-reveal lottery + Mock VRF.
        //     On mainnet, replace MockRandomness with a Chainlink VRF v2.5 adapter.
        MockRandomness rng = new MockRandomness();
        ArdiEpochDraw epochDraw = new ArdiEpochDraw(
            owner, vaultRoot, address(rng), coordinator, treasury
        );

        // 4. Wire-up
        nft.setBondEscrow(address(escrow));
        nft.setEpochDraw(address(epochDraw));
        escrow.setArdiNFT(address(nft));

        // 5. Initial LP mint
        token.mintLp(lpEscrow, 1_000_000_000 ether);

        // 6. Hand emission to MintController and lock
        token.setMinter(address(ctrl));
        token.lockMinter();

        // 7. Defensive: never let fusionPool == escrow (would lock tokens forever).
        require(escrow.fusionPool() != address(escrow), "fusionPool == escrow (locked)");
        require(address(nft.epochDraw()) == address(epochDraw), "nft epochDraw not wired");
        require(ctrl.ownerOpsAddr() == ownerOpsAddr, "ownerOpsAddr mismatch");
        require(ctrl.coordinator() == coordinator, "coordinator mismatch");

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
        console2.log("===== DEPLOYED =====");
        console2.log("MockAWP         :", out.mockAWP);
        console2.log("MockKYA         :", out.mockKYA);
        console2.log("ArdiToken       :", out.ardiToken);
        console2.log("ArdiNFT         :", out.ardiNFT);
        console2.log("ArdiBondEscrow  :", out.bondEscrow);
        console2.log("ArdiOTC         :", out.otc);
        console2.log("ArdiMintCtrl    :", out.mintController);
        console2.log("ArdiEpochDraw   :", out.epochDraw);
        console2.log("MockRandomness  :", out.mockRandomness);

        // Write addresses to a file for downstream scripts
        string memory deployJson = string.concat(
            "{\n",
            '  "chainId": ', vm.toString(block.chainid), ",\n",
            '  "owner": "', vm.toString(owner), '",\n',
            '  "coordinator": "', vm.toString(coordinator), '",\n',
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
        vm.writeFile("./deployments/local.json", deployJson);
    }
}
