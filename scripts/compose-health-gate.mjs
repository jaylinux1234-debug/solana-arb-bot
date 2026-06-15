#!/usr/bin/env node
/**
 * Never restart blindly: print ps + monitor logs; fail if stack is unhealthy.
 * Skip gate on first deploy (no running monitor). Override: COMPOSE_SKIP_HEALTH_GATE=1
 */
import { spawnSync } from "node:child_process";
import { rootDir } from "./compose-files.mjs";
import { COMPOSE_MONITORING } from "./compose-secret-files.mjs";

const COMPOSE_BASE = COMPOSE_MONITORING;

const skip = ["1", "true", "yes"].includes(
  (process.env.COMPOSE_SKIP_HEALTH_GATE || "").toLowerCase(),
);

function run(args, { capture = false } = {}) {
  return spawnSync("docker", [...COMPOSE_BASE, ...args], {
    cwd: rootDir,
    encoding: capture ? "utf8" : undefined,
    stdio: capture ? "pipe" : "inherit",
    shell: false,
  });
}

function parseHealth(psOut) {
  const bad = [];
  for (const line of psOut.split(/\r?\n/)) {
    const t = line.trim();
    if (!t) continue;
    let row;
    try {
      row = JSON.parse(t);
    } catch {
      continue;
    }
    const health = String(row.Health || "").toLowerCase();
    const state = String(row.State || row.Status || "").toLowerCase();
    const name = row.Name || row.Service || "unknown";
    if (health === "unhealthy" || state.includes("unhealthy")) {
      bad.push(name);
    }
  }
  return bad;
}

console.log("=== compose health gate ===\n");
run(["ps"]);
const ps = run(["ps", "--format", "json"], { capture: true });
const psText = `${ps.stdout || ""}${ps.stderr || ""}`;
if ((ps.status ?? 1) !== 0 && !psText.trim()) {
  console.error("docker compose ps --format json failed");
  process.exit(ps.status ?? 1);
}

const hasMonitor = /\bsolana-arb-monitor\b/i.test(psText);
if (!hasMonitor) {
  console.log("  (no running monitor — first deploy, gate skipped)\n");
  process.exit(0);
} else {

  console.log("\n--- monitor logs (last 100) ---\n");
  const logs = run(["logs", "monitor", "--tail", "100"]);
  if ((logs.status ?? 1) !== 0) process.exit(logs.status ?? 1);

  if (skip) {
    console.log("\n  COMPOSE_SKIP_HEALTH_GATE set — not failing on unhealthy status\n");
    process.exit(0);
  }

  const bad = parseHealth(psText);
  if (bad.length) {
    console.error(`\nERROR: unhealthy services: ${bad.join(", ")}`);
    console.error("  Fix health before restart. Override: COMPOSE_SKIP_HEALTH_GATE=1\n");
    process.exit(1);
  }

  console.log("\n  health gate OK\n");
}
