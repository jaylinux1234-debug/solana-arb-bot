#!/usr/bin/env node
import { spawnSync } from "node:child_process";
import { COMPOSE_MONITORING } from "./compose-secret-files.mjs";
import { rootDir } from "./compose-files.mjs";

const isWin = process.platform === "win32";

function run(cmd, args, { optional = false } = {}) {
  const r = spawnSync(cmd, args, {
    cwd: rootDir,
    stdio: "inherit",
    shell: isWin && cmd !== "docker",
  });
  if (!optional && (r.status ?? 1) !== 0) process.exit(r.status ?? 1);
}

/** Release stale Redis lock before recreate so --wait does not fail on restart races. */
function clearSingletonLock() {
  run("docker", [...COMPOSE_MONITORING, "stop", "monitor"], { optional: true });
  run("node", ["scripts/clear-singleton-lock.mjs"], { optional: true });
}

run("node", ["scripts/compose-health-gate.mjs"]);
run("node", ["scripts/sync-compose-env.mjs"]);
clearSingletonLock();
run("docker", [...COMPOSE_MONITORING, "up", "--build", "-d", "--wait"]);

console.log("\n=== prod + monitoring stack healthy (compose --wait) ===");
console.log("  Grafana:     http://localhost:3000");
console.log("  Prometheus:  http://localhost:9090");
console.log("  Health:      http://localhost:8000/health");
console.log("  Status:      npm run compose:ps:monitoring");
console.log("  Logs:        npm run compose:logs\n");
