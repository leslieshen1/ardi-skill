// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title IKYA — Know Your Agent attestation interface
/// @notice External interface to the KYA WorkNet (AIP-3). Ardi calls this at miner
///         registration to verify a wallet is a verified, unique agent identity.
/// @dev    The actual KYA contract lives on AWP RootNet. Ardi only consumes its
///         attestation and slash signals; it does not own the identity layer.
interface IKYA {
    /// @notice Returns true if `agent` holds a valid, non-revoked KYA attestation.
    /// @dev    Implementations may check biometric / social / economic attestations
    ///         per AIP-3. Reverts MUST NOT leak attestation type to preserve privacy.
    function isVerified(address agent) external view returns (bool);

    /// @notice Returns true if KYA has flagged `agent` as part of a sybil cluster.
    /// @dev    Used by ArdiBondEscrow.slashOnSybil to confirm a slash is justified.
    function isSybilFlagged(address agent) external view returns (bool);
}
