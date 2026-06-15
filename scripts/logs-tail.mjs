#!/usr/bin/env node
/**
 * npm run logs:tail — follow monitor via compose (preferred).
 * npm run logs:tail -- --since 5m  — recent window (no follow)
 * npm run logs:follow — direct docker logs -f solana-arb-monitor
 */
import { spawnSync } from "node:child_process";
import { COMPOSE_BASE, COMPOSE_SIMULATE, rootDir } from "./compose-files.mjs";
import { COMPOSE_MONITORING } from "./compose-secret-files.mjs";

const FLAGS = new Set(["--simulate", "--no-monitoring", "--direct"]);
const argv = process.argv.slice(2);
const simulate = argv.includes("--simulate");
const noMonitoring = argv.includes("--no-monitoring");
const direct = argv.includes("--direct");
const extraArgs = argv.filter((a) => !FLAGS.has(a));
const sinceMode = extraArgs.some((a) => a === "--since" || a.startsWith("--since="));
const tail = process.env.LOG_TAIL || "100";
const container = process.env.MONITOR_CONTAINER || "solana-arb-monitor";

const logArgs = [...extraArgs];
if (!sinceMode) {
  logArgs.unshift("-f");
}
if (!extraArgs.some((a) => a.startsWith("--tail"))) {
  logArgs.push("--tail", tail);
}

if (direct) {
  const r = spawnSync(
    "docker",
    ["logs", ...logArgs, container],
    { cwd: rootDir, stdio: "inherit", shell: false },
  );
  process.exit(r.status ?? 1);
}

const files = simulate
  ? COMPOSE_SIMULATE
  : noMonitoring
    ? COMPOSE_BASE
    : COMPOSE_MONITORING;

const r = spawnSync(
  "docker",
  [...files, "logs", ...logArgs, "monitor"],
  { cwd: rootDir, stdio: "inherit", shell: false },
);

process.exit(r.status ?? 1);
