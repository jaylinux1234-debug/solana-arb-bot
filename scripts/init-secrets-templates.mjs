#!/usr/bin/env node
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const isWin = process.platform === "win32";
const script = path.join(
  root,
  "scripts",
  isWin ? "init-secrets-templates.ps1" : "init-secrets-templates.sh",
);
const cmd = isWin ? "powershell" : "bash";
const args = isWin
  ? ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script]
  : [script];

const r = spawnSync(cmd, args, { cwd: root, stdio: "inherit", shell: false });
process.exit(r.status ?? 1);
