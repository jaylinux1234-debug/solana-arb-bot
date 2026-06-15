#!/usr/bin/env node
/**
 * Basescan verify ArbMonitorRegistry on base-mainnet.
 * Address: FLASH_LOAN_CONTRACT or ARB_MONITOR_REGISTRY_ADDRESS in .env / .env.txt
 */
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import dotenv from "dotenv";

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));

for (const name of [".env", "compose.env", ".env.txt", ".ENV.txt"]) {
  const p = path.join(root, name);
  if (fs.existsSync(p)) dotenv.config({ path: p, override: true });
}

const network = process.env.VERIFY_NETWORK || "base-mainnet";
const address = (
  process.env.ARB_MONITOR_REGISTRY_ADDRESS ||
  process.env.FLASH_LOAN_CONTRACT ||
  process.env.REGISTRY_ADDRESS ||
  ""
).trim();

if (!address || !/^0x[a-fA-F0-9]{40}$/.test(address)) {
  console.error(
    "ERROR: Set FLASH_LOAN_CONTRACT or ARB_MONITOR_REGISTRY_ADDRESS (0x…) in .env.txt",
  );
  process.exit(1);
}

const apiKey = (
  process.env.BASESCAN_API_KEY || process.env.ETHERSCAN_API_KEY || ""
).trim();
if (!apiKey) {
  console.error("ERROR: Set BASESCAN_API_KEY in .env or .env.txt");
  process.exit(1);
}

const version =
  process.env.DEPLOY_REGISTRY_VERSION?.trim() || "solana-arb-bot-monitor-1";
const owner = (
  process.env.GNOSIS_SAFE_ADDRESS ||
  process.env.TIMELOCK_ADDRESS ||
  process.env.HARDWARE_ADDRESS ||
  process.env.LEDGER_DEPLOYER_ADDRESS ||
  ""
).trim();

if (!owner || !/^0x[a-fA-F0-9]{40}$/.test(owner)) {
  console.error(
    "ERROR: Set GNOSIS_SAFE_ADDRESS or HARDWARE_ADDRESS (constructor owner) in .env.txt",
  );
  process.exit(1);
}

const force = process.argv.includes("--force") ? ["--force"] : [];

console.log(`=== verify:base network=${network} ===`);
console.log(`  contract: ${address}`);
console.log(`  version:  ${version}`);
console.log(`  owner:    ${owner}\n`);

const r = spawnSync(
  "npx",
  [
    "hardhat",
    "verify",
    "etherscan",
    "--network",
    network,
    ...force,
    address,
    version,
    owner,
  ],
  { cwd: root, stdio: "inherit", shell: false },
);

process.exit(r.status ?? 1);
