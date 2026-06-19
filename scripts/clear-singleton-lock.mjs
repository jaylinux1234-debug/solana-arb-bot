#!/usr/bin/env node
/** Clear Redis singleton lock on bot-redis or legacy bot-redis-prod. */
import { spawnSync } from "node:child_process";

const KEYS = [
  process.env.BOT_SINGLETON_NEXTLEVEL_KEY || "bot:singleton:nextlevel",
  process.env.V2_SINGLETON_KEY || "bot:singleton:v2",
  process.env.BOT_SINGLETON_ID
    ? `bot:singleton:${process.env.BOT_SINGLETON_ID}`
    : "bot:singleton:solana-arb-monitor",
];

let cleared = 0;
for (const container of ["bot-redis", "bot-redis-prod"]) {
  for (const key of KEYS) {
    const r = spawnSync(
      "docker",
      ["exec", container, "redis-cli", "DEL", key],
      { stdio: "pipe", encoding: "utf8" },
    );
    if ((r.status ?? 1) === 0 && (r.stdout || "").trim() === "1") {
      console.log(`Cleared singleton lock on ${container}: ${key}`);
      cleared += 1;
    }
  }
}

if (cleared > 0) {
  process.exit(0);
}

console.warn(`Singleton lock not cleared (containers may be down): ${KEYS.join(", ")}`);
