#!/usr/bin/env node
/** Cross-platform: npm run deps:lock */
import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));

/** Avoid shell:true (Node DEP0190); uv/python must be on PATH. */
function spawn(cmd, args, { inherit = true } = {}) {
  return spawnSync(cmd, args, {
    cwd: root,
    stdio: inherit ? "inherit" : "pipe",
    shell: false,
  });
}

function resolveUv() {
  if ((spawn("uv", ["--version"], { inherit: false }).status ?? 1) === 0) {
    return { cmd: "uv", prefix: [] };
  }
  if ((spawn("python", ["-m", "uv", "--version"], { inherit: false }).status ?? 1) === 0) {
    return { cmd: "python", prefix: ["-m", "uv"] };
  }
  console.log("Installing uv…\n");
  const pip = spawn("python", ["-m", "pip", "install", "--upgrade", "uv"]);
  if ((pip.status ?? 1) !== 0) process.exit(pip.status ?? 1);
  return { cmd: "python", prefix: ["-m", "uv"] };
}

function runUv(uv, pipArgs) {
  const r = spawn(uv.cmd, [...uv.prefix, ...pipArgs]);
  if ((r.status ?? 1) !== 0) process.exit(r.status ?? 1);
}

const uv = resolveUv();
const extra = process.argv.slice(2);

console.log("=== uv pip compile (prod, linux/3.11) ===\n");
runUv(uv, [
  "pip",
  "compile",
  "requirements.txt",
  "-o",
  "requirements.lock",
  "--python-platform",
  "linux",
  "--python-version",
  "3.11",
  ...extra,
]);

console.log("\n=== uv pip compile (dev) ===\n");
runUv(uv, [
  "pip",
  "compile",
  "requirements-dev.txt",
  "-o",
  "requirements-dev.lock",
  ...extra,
]);

console.log("\nDone.");
console.log("  Prod: uv pip sync requirements.lock");
console.log("  Dev:  uv pip sync requirements-dev.lock");
