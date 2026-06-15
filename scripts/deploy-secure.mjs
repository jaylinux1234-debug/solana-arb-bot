#!/usr/bin/env node
/** Cross-platform: npm run deploy:secure */
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const network = process.argv[2] || "base-mainnet";
const isWin = process.platform === "win32";

function run(cmd, args) {
  const r = spawnSync(cmd, args, { cwd: root, stdio: "inherit", shell: false });
  process.exit(r.status ?? 1);
}

if (isWin) {
  run("powershell", [
    "-NoProfile",
    "-ExecutionPolicy",
    "Bypass",
    "-File",
    path.join(root, "scripts", "deploy-secure.ps1"),
    network,
  ]);
} else {
  run("bash", [path.join(root, "scripts", "deploy-secure.sh"), network]);
}
