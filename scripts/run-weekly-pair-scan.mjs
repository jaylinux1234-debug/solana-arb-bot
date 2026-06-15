#!/usr/bin/env node
/**
 * Weekly CEX-DEX pair scan — logs to logs/weekly_pair_scan.jsonl
 *
 * Schedule (Windows Task Scheduler example):
 *   Program: npm
 *   Args: run scan:pairs:weekly
 *   Start in: C:\Users\jaypa\solana-arb-bot
 *   Trigger: Weekly
 */
import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const py = process.platform === "win32" ? "python" : "python3";

const r = spawnSync(
  py,
  [
    "-m",
    "src.scripts.multi_pair_scanner",
    "--weekly-log",
    "logs/weekly_pair_scan.jsonl",
  ],
  {
    cwd: ROOT,
    stdio: "inherit",
    env: {
      ...process.env,
      PYTHONPATH: ROOT,
      PYTHONIOENCODING: "utf-8",
    },
  },
);

process.exit(r.status ?? 1);
