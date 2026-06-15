#!/usr/bin/env node
/** Seed backtest_results/sim_trades.jsonl via spread scenarios (no RPC key required). */
import { spawnSync } from "node:child_process";
import { rootDir } from "./compose-files.mjs";

const grossValues = [];
for (let bps = 42; bps <= 92; bps += 1) {
  grossValues.push(bps);
}

const size = process.env.CEX_DEX_SIM_BATCH_FLASH_USDC_MICRO || "35000000";
let ok = 0;

for (const gross of grossValues) {
  const py = process.platform === "win32" ? "python" : "python3";
  const r = spawnSync(
    py,
    [
      "src/scripts/cex_dex_sim_batch.py",
      "--scenario-only",
      "--gross-bps",
      String(gross),
      "--size",
      size,
    ],
    {
      cwd: rootDir,
      stdio: "inherit",
      env: {
        ...process.env,
        PYTHONPATH: rootDir,
        PYTHONIOENCODING: "utf-8",
      },
    },
  );
  if ((r.status ?? 1) === 0) ok += 1;
}

console.log(`\nScenarios complete: ${ok}/${grossValues.length}`);
process.exit(ok === grossValues.length ? 0 : 1);
