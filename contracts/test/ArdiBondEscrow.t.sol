// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {ArdiBondEscrow} from "../src/ArdiBondEscrow.sol";
import {MockAWP, MockKYA} from "./Mocks.sol";

contract ArdiBondEscrowTest is Test {
    ArdiBondEscrow escrow;
    MockAWP awp;
    MockKYA kya;

    address owner = address(0xA11CE);
    address ardiNFT = address(0xCAFE);
    address fusionPool = address(0xBADBED);
    address agent = address(0xBEEF);
    address agent2 = address(0xDEAD);

    uint256 constant BOND = 10_000 ether;

    function setUp() public {
        awp = new MockAWP();
        kya = new MockKYA();

        vm.prank(owner);
        escrow = new ArdiBondEscrow(owner, address(awp), address(kya), fusionPool);

        vm.startPrank(owner);
        escrow.setArdiNFT(ardiNFT);
        vm.stopPrank();

        // Fund the agent
        awp.transfer(agent, 100_000 ether);
        awp.transfer(agent2, 100_000 ether);
    }

    function test_register_happyPath() public {
        kya.setVerified(agent, true);

        vm.startPrank(agent);
        awp.approve(address(escrow), BOND);
        escrow.registerMiner();
        vm.stopPrank();

        assertEq(awp.balanceOf(address(escrow)), BOND);
        assertTrue(escrow.isMiner(agent));

        (uint128 b,, uint8 mc, bool active, bool slashed) = escrow.miners(agent);
        assertEq(uint256(b), BOND);
        assertEq(mc, 0);
        assertTrue(active);
        assertFalse(slashed);
    }

    function test_register_revertsIfNotKYA() public {
        vm.startPrank(agent);
        awp.approve(address(escrow), BOND);
        vm.expectRevert(ArdiBondEscrow.NotKYAVerified.selector);
        escrow.registerMiner();
        vm.stopPrank();
    }

    function test_register_revertsIfAlreadyRegistered() public {
        kya.setVerified(agent, true);

        vm.startPrank(agent);
        awp.approve(address(escrow), BOND * 2);
        escrow.registerMiner();
        vm.expectRevert(ArdiBondEscrow.AlreadyRegistered.selector);
        escrow.registerMiner();
        vm.stopPrank();
    }

    function test_onMinted_byArdiNFT() public {
        kya.setVerified(agent, true);
        vm.startPrank(agent);
        awp.approve(address(escrow), BOND);
        escrow.registerMiner();
        vm.stopPrank();

        vm.prank(ardiNFT);
        escrow.onMinted(agent);

        (,, uint8 mc,,) = escrow.miners(agent);
        assertEq(mc, 1);
    }

    function test_onMinted_revertsIfNotArdiNFT() public {
        kya.setVerified(agent, true);
        vm.startPrank(agent);
        awp.approve(address(escrow), BOND);
        escrow.registerMiner();
        vm.stopPrank();

        vm.expectRevert(ArdiBondEscrow.NotArdiNFT.selector);
        escrow.onMinted(agent);
    }

    function test_unlock_afterCapReached() public {
        kya.setVerified(agent, true);
        vm.startPrank(agent);
        awp.approve(address(escrow), BOND);
        escrow.registerMiner();
        vm.stopPrank();

        // Simulate 3 mints
        vm.startPrank(ardiNFT);
        escrow.onMinted(agent);
        escrow.onMinted(agent);
        escrow.onMinted(agent);
        vm.stopPrank();

        uint256 balBefore = awp.balanceOf(agent);
        vm.prank(agent);
        escrow.unlockBond();
        assertEq(awp.balanceOf(agent), balBefore + BOND);
        assertFalse(escrow.isMiner(agent));
    }

    function test_unlock_revertsBeforeCapAndBeforeSeal() public {
        kya.setVerified(agent, true);
        vm.startPrank(agent);
        awp.approve(address(escrow), BOND);
        escrow.registerMiner();
        vm.stopPrank();

        vm.expectRevert(ArdiBondEscrow.StillLocked.selector);
        vm.prank(agent);
        escrow.unlockBond();
    }

    function test_unlock_afterSealAndCooldown() public {
        kya.setVerified(agent, true);
        vm.startPrank(agent);
        awp.approve(address(escrow), BOND);
        escrow.registerMiner();
        vm.stopPrank();

        vm.prank(ardiNFT);
        escrow.setSealed();

        // Pre-cooldown: still locked
        vm.expectRevert(ArdiBondEscrow.StillLocked.selector);
        vm.prank(agent);
        escrow.unlockBond();

        // Post-cooldown: unlocked
        vm.warp(block.timestamp + 25 hours);
        uint256 balBefore = awp.balanceOf(agent);
        vm.prank(agent);
        escrow.unlockBond();
        assertEq(awp.balanceOf(agent), balBefore + BOND);
    }

    function test_slash_byKYA() public {
        kya.setVerified(agent, true);
        vm.startPrank(agent);
        awp.approve(address(escrow), BOND);
        escrow.registerMiner();
        vm.stopPrank();

        kya.setSybil(agent, true);

        uint256 awpSupplyBefore = awp.totalSupply();

        vm.prank(address(kya));
        escrow.slashOnSybil(agent, 5000); // 50%

        // 25% burned, 25% to fusion pool, 50% refunded
        assertEq(awp.totalSupply(), awpSupplyBefore - 2500 ether);
        assertEq(awp.balanceOf(fusionPool), 2500 ether);

        (uint128 b,,,, bool slashed) = escrow.miners(agent);
        assertEq(uint256(b), 0);
        assertTrue(slashed);
        assertFalse(escrow.isMiner(agent));
    }

    function test_slash_full100pct() public {
        kya.setVerified(agent, true);
        vm.startPrank(agent);
        awp.approve(address(escrow), BOND);
        escrow.registerMiner();
        vm.stopPrank();

        kya.setSybil(agent, true);
        uint256 supplyBefore = awp.totalSupply();
        vm.prank(address(kya));
        escrow.slashOnSybil(agent, 10_000); // 100%

        // 50% burn (5000), 50% to pool (5000), 0 refund
        assertEq(awp.totalSupply(), supplyBefore - 5000 ether);
        assertEq(awp.balanceOf(fusionPool), 5000 ether);
    }

    function test_slash_revertsIfNotSybilFlagged() public {
        kya.setVerified(agent, true);
        vm.startPrank(agent);
        awp.approve(address(escrow), BOND);
        escrow.registerMiner();
        vm.stopPrank();

        // not flagged
        vm.prank(address(kya));
        vm.expectRevert(ArdiBondEscrow.NotSybil.selector);
        escrow.slashOnSybil(agent, 5000);
    }

    function test_slash_revertsIfNotAuthorized() public {
        kya.setVerified(agent, true);
        vm.startPrank(agent);
        awp.approve(address(escrow), BOND);
        escrow.registerMiner();
        vm.stopPrank();

        kya.setSybil(agent, true);

        vm.expectRevert(ArdiBondEscrow.NotKYAOrOwner.selector);
        escrow.slashOnSybil(agent, 5000);
    }
}
