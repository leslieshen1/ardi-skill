// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {IKYA} from "../src/interfaces/IKYA.sol";

/// @notice Mock ERC-20 used in tests as $AWP. Includes a burn() method so
///         ArdiBondEscrow's slash path exercises the burn branch.
contract MockAWP is ERC20 {
    constructor() ERC20("AWP-mock", "AWP") {
        _mint(msg.sender, 100_000_000 ether);
    }

    function burn(uint256 amount) external {
        _burn(msg.sender, amount);
    }

    function mint(address to, uint256 amount) external {
        _mint(to, amount);
    }
}

/// @notice Mock ArdiEpochDraw for ArdiNFT/inscribe unit tests. Lets a test
///         arbitrarily declare "agent X won (epoch, wordId) with answer (word,
///         power, lang)". The real ArdiEpochDraw is exercised separately in
///         ArdiEpochDraw.t.sol.
contract MockEpochDraw {
    struct Ans {
        string word;
        uint16 power;
        uint8 languageId;
        bool published;
    }

    mapping(uint256 => mapping(uint256 => address)) public winners;
    mapping(uint256 => mapping(uint256 => Ans)) private _answers;

    function setWinner(uint256 epochId, uint256 wordId, address w) external {
        winners[epochId][wordId] = w;
    }

    function setAnswer(uint256 epochId, uint256 wordId, string calldata word, uint16 power, uint8 languageId)
        external
    {
        _answers[epochId][wordId] = Ans(word, power, languageId, true);
    }

    function getAnswer(uint256 epochId, uint256 wordId)
        external
        view
        returns (string memory word, uint16 power, uint8 languageId, bool published)
    {
        Ans memory a = _answers[epochId][wordId];
        return (a.word, a.power, a.languageId, a.published);
    }
}

/// @notice Mock KYA contract for tests. Lets test code arbitrarily set per-agent
///         verification + sybil flags.
contract MockKYA is IKYA {
    mapping(address => bool) public verified;
    mapping(address => bool) public sybil;

    function setVerified(address agent, bool v) external {
        verified[agent] = v;
    }

    function setSybil(address agent, bool s) external {
        sybil[agent] = s;
    }

    function isVerified(address agent) external view returns (bool) {
        return verified[agent];
    }

    function isSybilFlagged(address agent) external view returns (bool) {
        return sybil[agent];
    }
}
