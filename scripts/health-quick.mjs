#!/usr/bin/env node
/** npm run health:quick — ps + /health curl */
import { spawnSync } from "node:child_process";
import { rootDir } from "./compose-files.mjs";
import { COMPOSE_MONITORING } from "./compose-secret-files.mjs";

function run(cmd, args, { inherit = true } = {}) {
  return spawnSync(cmd, args, {
    cwd: rootDir,
    stdio: inherit ? "inherit" : "pipe",
    encoding: inherit ? undefined : "utf8",
    shell: false,
  });
}

console.log("=== health:quick ===\n");
console.log("--- docker compose ps ---\n");
run("docker", [...COMPOSE_MONITORING, "ps"]);

const ports = [
  process.env.V2_HEALTH_PORT,
  process.env.BOT_HEALTH_PORT,
  "8001",
  "8000",
]
  .filter(Boolean)
  .map((p) => String(p).trim())
  .filter((p, i, arr) => arr.indexOf(p) === i);

for (const port of ports) {
  const url = `http://127.0.0.1:${port}/health`;
  console.log(`\n--- curl ${url} ---\n`);
  const curl = run("curl", ["-sf", url], { inherit: false });
  if ((curl.status ?? 1) === 0) {
    console.log((curl.stdout || "").trim() || "ok");
    console.log(`\nMonitor healthy (port ${port})`);
    process.exit(0);
  }
}

console.error("Monitor unhealthy (tried ports: " + ports.join(", ") + ")");
process.exit(1);
