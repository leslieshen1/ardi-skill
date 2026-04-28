// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

interface IArdiBondEscrow {
    function isMiner(address agent) external view returns (bool);
    function onMinted(address agent) external; // hook to update bond state
}
