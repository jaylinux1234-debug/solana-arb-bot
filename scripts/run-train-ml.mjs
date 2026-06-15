#!/usr/bin/env node
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { rootDir } from "./compose-files.mjs";

const venvPy =
  process.platform === "win32"
    ? path.join(rootDir, "venv", "Scripts", "python.exe")
    : path.join(rootDir, "venv", "bin", "python3");
const py = fs.existsSync(venvPy) ? venvPy : process.platform === "win32" ? "python" : "python3";

const extra = process.argv.slice(2).filter((a) => a !== "--");
const args = ["scripts/train_ml_backtest.py", ...extra];
if (!extra.includes("--ensemble") && !extra.includes("--real-fills-only")) {
  args.push("--ensemble", "--min-samples", "30");
}
const r = spawnSync(py, args, {
  cwd: rootDir,
  stdio: "inherit",
  env: { ...process.env, PYTHONIOENCODING: "utf-8" },
});
process.exit(r.status ?? 1);
