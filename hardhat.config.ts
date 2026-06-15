import { defineConfig } from "hardhat/config";
import hardhatIgnition from "@nomicfoundation/hardhat-ignition";
import hardhatToolboxMochaEthers from "@nomicfoundation/hardhat-toolbox-mocha-ethers";
import hardhatVerify from "@nomicfoundation/hardhat-verify";
import dotenv from "dotenv";

dotenv.config();

const BASE_MAINNET_CHAIN_ID = 8453;
const BASE_SEPOLIA_CHAIN_ID = 84532;

function envRpc(...keys: string[]): string | undefined {
  for (const key of keys) {
    const v = process.env[key]?.trim();
    if (v) return v;
  }
  return undefined;
}

function baseMainnetUrl(): string {
  return (
    envRpc("BASE_RPC", "ALCHEMY_RPC", "QUICKNODE_RPC") ??
    (process.env.ALCHEMY_KEY
      ? `https://base-mainnet.g.alchemy.com/v2/${process.env.ALCHEMY_KEY}`
      : "https://mainnet.base.org")
  );
}

function baseSepoliaUrl(): string {
  return (
    envRpc("BASE_SEPOLIA_RPC", "BASE_RPC_SEPOLIA") ??
    (process.env.ALCHEMY_KEY
      ? `https://base-sepolia.g.alchemy.com/v2/${process.env.ALCHEMY_KEY}`
      : "https://sepolia.base.org")
  );
}

/** Optional Base deploy key — separate from Solana hot wallet; never commit. */
function deployerAccounts(): string[] {
  const pk =
    process.env.DEPLOYER_PRIVATE_KEY?.trim() ||
    process.env.BASE_DEPLOYER_PRIVATE_KEY?.trim() ||
    "";
  return pk ? [pk] : [];
}

const deployAccounts = deployerAccounts();

const basescanKey =
  process.env.BASESCAN_API_KEY?.trim() ||
  process.env.ETHERSCAN_API_KEY?.trim() ||
  "";

const deployGas = {
  gasPrice: "auto" as const,
  gasMultiplier: 1.2,
};

export default defineConfig({
  plugins: [hardhatToolboxMochaEthers, hardhatIgnition, hardhatVerify],
  solidity: {
    version: "0.8.27",
    settings: {
      optimizer: { enabled: true, runs: 200 },
    },
  },
  networks: {
    "base-mainnet": {
      type: "http",
      chainId: BASE_MAINNET_CHAIN_ID,
      url: baseMainnetUrl(),
      ...deployGas,
      ...(deployAccounts.length > 0 ? { accounts: deployAccounts } : {}),
    },
    "base-sepolia": {
      type: "http",
      chainId: BASE_SEPOLIA_CHAIN_ID,
      url: baseSepoliaUrl(),
      ...deployGas,
      ...(deployAccounts.length > 0 ? { accounts: deployAccounts } : {}),
    },
    hardhat: {
      type: "edr-simulated",
      chainType: "op",
    },
  },
  verify: {
    etherscan: {
      apiKey: basescanKey,
    },
  },
  chainDescriptors: {
    [BASE_MAINNET_CHAIN_ID]: {
      name: "base",
      blockExplorers: {
        etherscan: {
          name: "Basescan",
          url: "https://basescan.org",
          apiUrl: "https://api.basescan.org/api",
        },
      },
    },
    [BASE_SEPOLIA_CHAIN_ID]: {
      name: "base-sepolia",
      blockExplorers: {
        etherscan: {
          name: "Basescan Sepolia",
          url: "https://sepolia.basescan.org",
          apiUrl: "https://api-sepolia.basescan.org/api",
        },
      },
    },
  },
});
