#!/usr/bin/env node
/**
 * Swarm stack deploy with rolling update_config (infra/compose/docker-compose.swarm.yml).
 */
import { spawnSync } from "node:child_process";
import { COMPOSE_FILE_PATHS_SWARM, rootDir } from "./compose-files.mjs";

const stack = process.env.SWARM_STACK || "solana-arb";

function run(cmd, args) {
  const r = spawnSync(cmd, args, { cwd: rootDir, stdio: "inherit", shell: false });
  if ((r.status ?? 1) !== 0) process.exit(r.status ?? 1);
}

const isUpdate = process.argv.includes("--update");

console.log(`=== swarm ${isUpdate ? "update" : "deploy"}: ${stack} ===\n`);
if (isUpdate) {
  run("node", ["scripts/compose-health-gate.mjs"]);
}
run("npm", ["run", "sync:compose-env"]);

const stackArgs = ["stack", "deploy"];
for (const f of COMPOSE_FILE_PATHS_SWARM) {
  stackArgs.push("--compose-file", f);
}
stackArgs.push(stack);

run("docker", stackArgs);

console.log("\n=== Swarm stack deployed (rolling updates on monitor) ===");
console.log(`  docker stack services ${stack}`);
console.log(`  docker service logs -f ${stack}_monitor\n`);
