#!/usr/bin/env node
/**
 * Safe restart: health gate → recreate monitor only (redis/metrics stay up).
 * Equivalent: docker compose ... up --build -d --force-recreate --wait monitor
 */
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";
import fs from "node:fs";
import { COMPOSE_MONITORING } from "./compose-secret-files.mjs";
import { rootDir } from "./compose-files.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const COMPOSE_BASE = COMPOSE_MONITORING;

const service = process.env.COMPOSE_SERVICE || "monitor";
const noBuild = process.argv.includes("--no-build");

function run(cmd, args) {
  const r = spawnSync(cmd, args, { cwd: rootDir, stdio: "inherit", shell: false });
  if ((r.status ?? 1) !== 0) process.exit(r.status ?? 1);
}

function healLogsOwnership() {
  const logsDir = path.join(rootDir, "logs");
  if (!fs.existsSync(logsDir)) return;
  // Production monitor runs as uid:gid 1000:1000; align logs to prevent v2.log PermissionError.
  const r = spawnSync("chown", ["-R", "1000:1000", logsDir], {
    cwd: rootDir,
    stdio: "inherit",
    shell: false,
  });
  if ((r.status ?? 0) !== 0) {
    console.warn("warn: unable to chown logs to 1000:1000; continuing restart");
  }
}

console.log(`=== safe restart: ${service} ===\n`);
if (!process.argv.includes("--with-gate")) {
  console.log("  (skipping health gate — monitor will be recreated)\n");
} else {
  run("node", ["scripts/compose-health-gate.mjs"]);
}
run("node", ["scripts/sync-compose-env.mjs"]);
healLogsOwnership();

/** Stale lock blocks startup until TTL (~120s); health /wait fails during wait. */
function clearSingletonLock() {
  const stop = spawnSync("docker", [...COMPOSE_BASE, "stop", service], {
    cwd: rootDir,
    stdio: "inherit",
    shell: false,
  });
  if ((stop.status ?? 1) !== 0) {
    /* monitor may already be stopped */
  }
  spawnSync("node", [path.join(__dirname, "clear-singleton-lock.mjs")], {
    cwd: rootDir,
    stdio: "inherit",
    shell: false,
  });
}

clearSingletonLock();

const upArgs = [...COMPOSE_BASE, "up"];
if (!noBuild) upArgs.push("--build");
upArgs.push("-d", "--force-recreate", "--wait", service);

run("docker", upArgs);

console.log(`\n=== ${service} recreated and healthy ===`);
console.log("  npm run compose:ps");
console.log("  npm run compose:logs\n");
