#!/usr/bin/env node
/**
 * Cross-platform entry for npm run go-live:secrets:sh
 * (Windows often has no /bin/bash — use PowerShell there.)
 */
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const isWin = process.platform === "win32";

const run = (cmd, args) => {
  const r = spawnSync(cmd, args, { cwd: root, stdio: "inherit", shell: false });
  process.exit(r.status ?? 1);
};

if (isWin) {
  run("powershell", [
    "-NoProfile",
    "-ExecutionPolicy",
    "Bypass",
    "-File",
    path.join(root, "scripts", "go-live-secrets.ps1"),
  ]);
} else {
  run("bash", [path.join(root, "scripts", "go-live-secrets.sh")]);
}
