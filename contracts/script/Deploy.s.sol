// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Script, console2} from "forge-std/Script.sol";
import {ArdiToken} from "../src/ArdiToken.sol";
import {ArdiNFT} from "../src/ArdiNFT.sol";
import {ArdiBondEscrow} from "../src/ArdiBondEscrow.sol";
import {ArdiOTC} from "../src/ArdiOTC.sol";
import {ArdiMintController} from "../src/ArdiMintController.sol";
import {ArdiEpochDraw} from "../src/ArdiEpochDraw.sol";

/// @notice Full Ardi WorkNet deployment script.
/// @dev    Reads required addresses + parameters from env:
///           OWNER_ADDR        — Ardi Owner (multisig recommended)
///           COORDINATOR_ADDR  — off-chain Coordinator signing key
///           AWP_TOKEN_ADDR    — $AWP ERC-20 on the target chain (Base mainnet/sepolia)
///           KYA_ADDR          — KYA registry contract
///           VAULT_MERKLE_ROOT — bytes32 Merkle root of the 21,000 vault entries
///           LP_ESCROW_ADDR    — destination for the 1B initial $aArdi LP mint
///           TREASURY_ADDR     — destination for slash proceeds (BondEscrow's fusionPool slot).
///                                MUST be a multisig / dedicated treasury contract — never the
///                                escrow itself (BondEscrow has no withdraw path, tokens sent
///                                there are unrecoverable).
///           OWNER_OPS_ADDR    — single EOA receiving the AWP ops cut from MintController
///                                (10% of daily AWP receipts by default, Timelock-adjustable).
///                                Withdraws always route to this address regardless of caller.
///           RANDOMNESS_ADDR   — IRandomnessSource implementation. On mainnet this MUST be a
///                                Chainlink VRF v2.5 adapter that wraps the official Coordinator
///                                (NOT MockRandomness, which is for local/test only). The deploy
///                                script does not deploy this — operator deploys + funds the
///                                Chainlink subscription separately and passes the adapter
///                                address here.
///           GENESIS_TS        — unix timestamp marking day 1 of emission (e.g. launch UTC 00:00)
///
///         Run:
///           forge script script/Deploy.s.sol --rpc-url $RPC_URL --broadcast
contract Deploy is Script {
    error InvalidTreasury();

    struct Addrs {
        address ardiToken;
        address ardiNFT;
        address bondEscrow;
        address otc;
        address mintController;
        address epochDraw;
    }

    function run() external returns (Addrs memory out) {
        address owner = vm.envAddress("OWNER_ADDR");
        address coordinator = vm.envAddress("COORDINATOR_ADDR");
        address awp = vm.envAddress("AWP_TOKEN_ADDR");
        address kya = vm.envAddress("KYA_ADDR");
        bytes32 vaultRoot = vm.envBytes32("VAULT_MERKLE_ROOT");
        address lpEscrow = vm.envAddress("LP_ESCROW_ADDR");
        address treasury = vm.envAddress("TREASURY_ADDR");
        address randomness = vm.envAddress("RANDOMNESS_ADDR");
        address ownerOpsAddr = vm.envAddress("OWNER_OPS_ADDR");
        uint256 genesisTs = vm.envUint("GENESIS_TS");

        // Defensive guard — see SECURITY.md "Deployment correctness".
        // Pointing the on-chain `fusionPool` slot at the escrow itself locks any
        // tokens routed there forever, because BondEscrow has no withdraw path.
        if (treasury == address(0)) revert InvalidTreasury();

        vm.startBroadcast();

        // 1. Deploy $ardi token
        ArdiToken token = new ArdiToken(owner);

        // 2. Deploy ArdiBondEscrow with treasury as the fusionPool slot.
        //    Under the default 100%-holder split (settlement holder_bps=10000,
        //    fusion_bps=0), the only thing that ever flows here is the 50%
        //    slash share from sybil-flagged miners.
        ArdiBondEscrow escrow = new ArdiBondEscrow(owner, awp, kya, treasury);

        // 3. Deploy ArdiNFT, wire to escrow
        ArdiNFT nft = new ArdiNFT(owner, coordinator, vaultRoot);

        // 4. Deploy OTC
        ArdiOTC otc = new ArdiOTC(owner, address(nft));

        // 5. Deploy MintController. AWP-aligned manager — DEFAULT_ADMIN_ROLE goes
        //    to `owner` (in production: a Timelock multisig); MERKLE_ROLE goes to
        //    the Coordinator key; OWNER_OPS_ROLE goes to the operator EOA. The
        //    operator's AWP ops cut routes to `ownerOpsAddr` regardless of caller.
        ArdiMintController ctrl = new ArdiMintController(
            owner,
            address(token),
            awp,
            coordinator,
            ownerOpsAddr,
            genesisTs
        );

        // 5b. Deploy on-chain commit-reveal lottery, wired to the operator-supplied
        //     IRandomnessSource (Chainlink VRF adapter on mainnet).
        ArdiEpochDraw epochDraw = new ArdiEpochDraw(
            owner, vaultRoot, randomness, coordinator, treasury
        );

        // 6. Wire-up: transfer ownership operations
        // Note: these calls happen during broadcast as the deployer. In production,
        // deployer should equal `owner`, OR we'd post-process via multisig.
        nft.setBondEscrow(address(escrow));
        nft.setEpochDraw(address(epochDraw));
        escrow.setArdiNFT(address(nft));

        // 7. Mint initial 1B $ardi LP supply
        token.mintLp(lpEscrow, 1_000_000_000 ether);

        // 8. Hand emission authority to MintController and lock it
        token.setMinter(address(ctrl));
        token.lockMinter();

        // 9. Defensive post-conditions — guard against future deploy-script regressions.
        require(escrow.fusionPool() != address(escrow), "fusionPool == escrow (locked)");
        require(address(nft.epochDraw()) == address(epochDraw), "epochDraw not wired");
        require(ctrl.ownerOpsAddr() == ownerOpsAddr, "ownerOpsAddr mismatch");
        require(ctrl.coordinator() == coordinator, "coordinator mismatch");

        vm.stopBroadcast();

        out = Addrs({
            ardiToken: address(token),
            ardiNFT: address(nft),
            bondEscrow: address(escrow),
            otc: address(otc),
            mintController: address(ctrl),
            epochDraw: address(epochDraw)
        });

        console2.log("ArdiToken         :", out.ardiToken);
        console2.log("ArdiNFT           :", out.ardiNFT);
        console2.log("ArdiBondEscrow    :", out.bondEscrow);
        console2.log("ArdiOTC           :", out.otc);
        console2.log("ArdiMintController:", out.mintController);
        console2.log("ArdiEpochDraw     :", out.epochDraw);
    }
}
