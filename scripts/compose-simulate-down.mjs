#!/usr/bin/env node
import { spawnSync } from "node:child_process";
import { COMPOSE_SIMULATE, rootDir } from "./compose-files.mjs";

const r = spawnSync("docker", [...COMPOSE_SIMULATE, "down"], {
  cwd: rootDir,
  stdio: "inherit",
  shell: false,
});
process.exit(r.status ?? 1);
