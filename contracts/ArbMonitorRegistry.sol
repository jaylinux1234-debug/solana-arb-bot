// SPDX-License-Identifier: MIT
pragma solidity ^0.8.27;

import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {Ownable2Step} from "@openzeppelin/contracts/access/Ownable2Step.sol";

/// @notice Version registry for Base monitor / ops. Owner should be a Gnosis Safe or timelock.
contract ArbMonitorRegistry is Ownable2Step {
    string public version;

    event VersionUpdated(string indexed oldVersion, string indexed newVersion);

    constructor(string memory initialVersion, address initialOwner) Ownable(initialOwner) {
        version = initialVersion;
    }

    function setVersion(string memory newVersion) external onlyOwner {
        string memory oldVersion = version;
        version = newVersion;
        emit VersionUpdated(oldVersion, newVersion);
    }
}
