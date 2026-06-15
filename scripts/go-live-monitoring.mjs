#!/usr/bin/env node
/**
 * Start redis + monitor + Prometheus + Grafana (metrics stack).
 * Grafana: http://localhost:3000  (default admin / admin from compose)
 * Prometheus: http://localhost:9090
 */
import { spawnSync } from "node:child_process";
import { rootDir } from "./compose-files.mjs";
import { COMPOSE_MONITORING } from "./compose-secret-files.mjs";

const SERVICES = ["redis", "monitor", "prometheus", "grafana"];

function run(cmd, args) {
  const r = spawnSync(cmd, args, { cwd: rootDir, stdio: "inherit", shell: false });
  if ((r.status ?? 1) !== 0) process.exit(r.status ?? 1);
}

console.log("=== go-live:monitoring ===\n");

const gate = spawnSync("node", ["scripts/compose-health-gate.mjs"], {
  cwd: rootDir,
  stdio: "inherit",
  shell: false,
});
if ((gate.status ?? 1) !== 0) {
  const skip = ["1", "true", "yes"].includes(
    (process.env.COMPOSE_SKIP_HEALTH_GATE || "").toLowerCase(),
  );
  if (!skip) process.exit(gate.status ?? 1);
}

run("npm", ["run", "sync:compose-env"]);

const upArgs = [...COMPOSE_MONITORING, "up", "--build", "-d"];
if (process.env.COMPOSE_WAIT !== "0") {
  upArgs.push("--wait");
}
run("docker", upArgs);

const grafanaPort = process.env.GRAFANA_PORT || "3000";
const promPort = process.env.PROMETHEUS_PORT || "9090";

console.log("\n=== Monitoring stack is up ===");
console.log(`  Grafana:     http://localhost:${grafanaPort}  (user: admin, password: see GRAFANA_ADMIN_PASSWORD in .env)`);
console.log(`  Prometheus:  http://localhost:${promPort}`);
console.log(`  Bot health:  http://localhost:${process.env.BOT_HEALTH_PORT || "8000"}/health`);
console.log(`  Bot metrics: http://localhost:${process.env.BOT_HEALTH_PORT || "8000"}/metrics`);
console.log("  Status:      npm run compose:ps");
console.log("  Logs:        npm run compose:logs\n");
