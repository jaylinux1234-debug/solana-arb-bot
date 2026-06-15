#!/usr/bin/env node
/**
 * npm run watch:24h — periodic health + error snapshot for first-day monitoring.
 */
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const intervalMin = Number(process.env.WATCH_INTERVAL_MIN || "15");
const logPath = path.join(
  root,
  "logs",
  `watch_24h_${new Date().toISOString().slice(0, 10).replace(/-/g, "")}.log`,
);

fs.mkdirSync(path.join(root, "logs"), { recursive: true });

function stamp() {
  return new Date().toISOString();
}

function append(line) {
  const text = `[${stamp()}] ${line}\n`;
  fs.appendFileSync(logPath, text, "utf8");
  process.stdout.write(text);
}

function runNpm(script) {
  return spawnSync("npm", ["run", script], {
    cwd: root,
    encoding: "utf8",
    shell: true,
    stdio: ["ignore", "pipe", "pipe"],
  });
}

async function tick() {
  append("--- health:quick ---");
  const h = runNpm("health:quick");
  if (h.stdout) append(h.stdout.trim());
  if (h.stderr) append(h.stderr.trim());
  if ((h.status ?? 1) !== 0) append(`health:quick exit ${h.status}`);

  append("--- logs:errors ---");
  const e = runNpm("logs:errors");
  if (e.stdout) append(e.stdout.trim());
  if ((e.status ?? 1) !== 0 && e.stderr) append(e.stderr.trim());
}

console.log(`watch:24h → ${logPath} (every ${intervalMin} min)\n`);
append("watch:24h started");
await tick();
setInterval(() => {
  tick().catch((err) => append(`tick error: ${err}`));
}, intervalMin * 60 * 1000);
