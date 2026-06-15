#!/usr/bin/env node
/**
 * Step 1: prod-shaped stack in simulation (TEST_MODE + SIMULATE).
 * Monitor 24h+: npm run logs:tail
 */
import { spawnSync } from "node:child_process";
import { COMPOSE_SIMULATE, rootDir } from "./compose-files.mjs";

const isWin = process.platform === "win32";

function run(cmd, args) {
  const r = spawnSync(cmd, args, { cwd: rootDir, stdio: "inherit", shell: isWin });
  if ((r.status ?? 1) !== 0) process.exit(r.status ?? 1);
}

console.log("=== go-live:simulate ===\n");
run("npm", ["run", "sync:compose-env"]);
run("node", ["scripts/compose-health-gate.mjs"]);
run("docker", [...COMPOSE_SIMULATE, "up", "--build", "-d", "--wait"]);

console.log("\n=== Simulation stack is up ===");
console.log("  Watch logs:  npm run logs:tail");
console.log("  Health:      curl http://localhost:8000/health");
console.log("  Stop:        npm run compose:simulate:down");
console.log("  After 24h+:  npm run go-live:preflight:sh && npm run compose:prod:up\n");
