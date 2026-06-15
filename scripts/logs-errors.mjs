#!/usr/bin/env node
/** npm run logs:errors — recent ERROR/Exception lines from monitor */
import { spawnSync } from "node:child_process";
import { rootDir } from "./compose-files.mjs";
import { COMPOSE_MONITORING } from "./compose-secret-files.mjs";

const tail = process.env.LOG_TAIL || "200";
const container = process.env.MONITOR_CONTAINER || "solana-arb-monitor";
const pattern = process.env.LOG_ERROR_PATTERN || "ERROR|Exception|Traceback|CRITICAL";

const useCompose = !process.argv.includes("--direct");

let text = "";
if (useCompose) {
  const r = spawnSync(
    "docker",
    [...COMPOSE_MONITORING, "logs", "monitor", "--tail", tail],
    { cwd: rootDir, encoding: "utf8", shell: false },
  );
  text = `${r.stdout || ""}${r.stderr || ""}`;
} else {
  const r = spawnSync(
    "docker",
    ["logs", "--tail", tail, container],
    { cwd: rootDir, encoding: "utf8", shell: false },
  );
  text = `${r.stdout || ""}${r.stderr || ""}`;
}

const re = new RegExp(pattern, "i");
const lines = text.split(/\r?\n/).filter((ln) => re.test(ln));

console.log(`=== logs:errors (last ${tail} lines, ${container}) ===\n`);
if (!lines.length) {
  console.log("(no matching lines)");
  process.exit(0);
}
for (const ln of lines) console.log(ln);
process.exit(0);
