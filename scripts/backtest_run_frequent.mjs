#!/usr/bin/env node
/**
 * Frequent CEX-DEX backtest loop: scenarios → optional sims → ML train.
 *
 *   npm run backtest:frequent
 *   BACKTEST_SIM_COUNT=10 npm run backtest:frequent
 */
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { rootDir } from "./compose-files.mjs";

const venvPy =
  process.platform === "win32"
    ? path.join(rootDir, "venv", "Scripts", "python.exe")
    : path.join(rootDir, "venv", "bin", "python3");
const py = fs.existsSync(venvPy) ? venvPy : process.platform === "win32" ? "python" : "python3";

const grossBps = process.env.BACKTEST_GROSS_BPS || "15";
const size = process.env.BACKTEST_SIZE_MICRO || "20000000";
const count = process.env.BACKTEST_SIM_COUNT || "0";

function run(args, label) {
  console.log(`\n=== ${label} ===`);
  const r = spawnSync(py, args, {
    cwd: rootDir,
    stdio: "inherit",
    env: { ...process.env, PYTHONPATH: rootDir, PYTHONIOENCODING: "utf-8" },
  });
  if ((r.status ?? 1) !== 0) {
    console.error(`${label} failed (exit ${r.status})`);
    process.exit(r.status ?? 1);
  }
}

run(
  [
    "scripts/backtest_tune_cex_dex.py",
    "--scenarios-only",
    "--gross-min",
    "8",
    "--gross-max",
    "22",
    "--gross-step",
    "1",
    "--size",
    size,
  ],
  "param tune (scenarios)",
);

if (Number(count) > 0) {
  run(
    [
      "src/scripts/cex_dex_sim_batch.py",
      "--count",
      count,
      "--mode",
      "jupiter_swap",
      "--gross-bps",
      grossBps,
      "--size",
      size,
    ],
    `Jupiter sim x${count}`,
  );
} else {
  run(
    [
      "src/scripts/cex_dex_sim_batch.py",
      "--scenario-only",
      "--gross-bps",
      grossBps,
      "--size",
      size,
    ],
    "spread scenario (no RPC)",
  );
}

run(["scripts/train_ml_backtest.py", "--min-samples", "20"], "ML train");

console.log("\nBacktest loop complete.");
