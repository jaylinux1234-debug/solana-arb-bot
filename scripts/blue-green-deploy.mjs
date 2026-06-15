#!/usr/bin/env node
/** Cross-platform launcher for scripts/blue_green_deploy.sh (Git Bash on Windows). */
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const candidates =
  process.platform === "win32"
    ? [
        process.env.GIT_BASH || "C:\\Program Files\\Git\\bin\\bash.exe",
        "C:\\Program Files\\Git\\usr\\bin\\bash.exe",
        "bash",
      ]
    : ["bash"];

const bash = candidates.find((p) => p === "bash" || existsSync(p));
if (!bash) {
  console.error("bash not found — install Git for Windows or use WSL.");
  process.exit(1);
}

const r = spawnSync(bash, ["scripts/blue_green_deploy.sh", ...process.argv.slice(2)], {
  cwd: root,
  stdio: "inherit",
  shell: false,
});
process.exit(r.status ?? 1);
