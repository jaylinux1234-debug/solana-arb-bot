#!/usr/bin/env node
/**
 * Local sim run — avoids port 8000/8799 conflicts with Docker monitor.
 * Usage: npm run sim:local [-- duration_seconds]
 */
import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const durationSec = Number.parseInt(process.argv[2] || "90", 10);
const python =
  process.platform === "win32"
    ? path.join(ROOT, "venv", "Scripts", "python.exe")
    : path.join(ROOT, "venv", "bin", "python");

if (!fs.existsSync(python)) {
  console.error("Missing venv python — run: npm run setup:install");
  process.exit(1);
}

const env = {
  ...process.env,
  PYTHONIOENCODING: "utf-8",
  TEST_MODE: "true",
  SIMULATE: "true",
  APP_ENV: "development",
  ENABLE_HELIUS_WEBHOOK: "false",
  ENABLE_BOT_HEALTH_SERVER: "false",
  METRICS_PROMETHEUS_PORT: "0",
  BOT_HEALTH_PORT: "8001",
  JUPITER_QUOTE_URL: "https://lite-api.jup.ag/swap/v1/quote",
  JUPITER_SWAP_URL: "https://lite-api.jup.ag/swap/v1/swap",
};

console.log(`=== sim:local (${durationSec}s) ===`);
console.log("Stop Docker monitor first if ports 8000/8799 are in use.");

const child = spawn(python, ["-m", "src.main"], {
  cwd: ROOT,
  env,
  stdio: ["ignore", "pipe", "pipe"],
});

const logPath = path.join(ROOT, "logs", "sim_run.log");
fs.mkdirSync(path.dirname(logPath), { recursive: true });
const logStream = fs.createWriteStream(logPath, { flags: "a" });
child.stdout.pipe(logStream);
child.stderr.pipe(logStream);
child.stdout.pipe(process.stdout);
child.stderr.pipe(process.stderr);

const timer = setTimeout(() => {
  console.log(`\n=== sim:local stopping after ${durationSec}s ===`);
  child.kill("SIGTERM");
  setTimeout(() => child.kill("SIGKILL"), 3000);
}, durationSec * 1000);

child.on("exit", (code, signal) => {
  clearTimeout(timer);
  logStream.end();
  const exitCode = code ?? (signal ? 1 : 0);
  process.exit(exitCode === null ? 1 : exitCode);
});
