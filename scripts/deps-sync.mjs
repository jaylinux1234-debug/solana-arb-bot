#!/usr/bin/env node
/** Cross-platform deps sync — uv when available; Windows skips Linux-only prod lock. */
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const isWin = process.platform === "win32";
const dev = process.argv.includes("--dev");
const lock = path.join(root, dev ? "requirements-dev.lock" : "requirements.lock");

function run(cmd, args) {
  const r = spawnSync(cmd, args, { cwd: root, stdio: "inherit", shell: false });
  return r.status ?? 1;
}

function pyCmd() {
  const venvPy = path.join(root, "venv", isWin ? "Scripts" : "bin", isWin ? "python.exe" : "python");
  return existsSync(venvPy) ? venvPy : "python";
}

const py = pyCmd();

if (isWin && !dev) {
  console.log("deps:sync | Windows: skipping requirements.lock (Linux/Docker parity).");
  console.log("deps:sync | Run: npm run deps:sync:dev  or  npm run setup:install:ps1");
  process.exit(run(py, ["-m", "uv", "pip", "sync", path.join(root, "requirements-dev.lock")]));
}

const uvCandidates = ["uv", path.join(root, "venv", isWin ? "Scripts" : "bin", isWin ? "uv.exe" : "uv")];
const uv = uvCandidates.find((c) => c === "uv" || existsSync(c));

if (uv) {
  process.exit(run(uv, ["pip", "sync", lock]));
}

const ensureUv = run(py, ["-m", "pip", "install", "uv"]);
if (ensureUv !== 0) {
  console.error("deps:sync | failed to install uv");
  process.exit(ensureUv);
}
process.exit(run(py, ["-m", "uv", "pip", "sync", lock]));
