#!/usr/bin/env node
/** npm run compose:restart:monitor — quick restart (no rebuild) */
import { spawnSync } from "node:child_process";
import { rootDir } from "./compose-files.mjs";
import { COMPOSE_MONITORING } from "./compose-secret-files.mjs";

console.log("=== restart monitor (compose restart) ===\n");
const r = spawnSync(
  "docker",
  [...COMPOSE_MONITORING, "restart", "monitor"],
  { cwd: rootDir, stdio: "inherit", shell: false },
);
process.exit(r.status ?? 1);
