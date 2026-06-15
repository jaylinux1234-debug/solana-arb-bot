import { buildModule } from "@nomicfoundation/hardhat-ignition/modules";

const deployVersion =
  process.env.DEPLOY_REGISTRY_VERSION?.trim() || "solana-arb-bot-monitor-1";

const owner =
  process.env.GNOSIS_SAFE_ADDRESS?.trim() ||
  process.env.TIMELOCK_ADDRESS?.trim() ||
  process.env.LEDGER_DEPLOYER_ADDRESS?.trim() ||
  "";

export default buildModule("ArbMonitorRegistryModule", (m) => {
  if (!owner) {
    throw new Error(
      "Set GNOSIS_SAFE_ADDRESS (preferred), TIMELOCK_ADDRESS, or LEDGER_DEPLOYER_ADDRESS",
    );
  }
  const registry = m.contract("ArbMonitorRegistry", [deployVersion, owner]);
  return { registry };
});
