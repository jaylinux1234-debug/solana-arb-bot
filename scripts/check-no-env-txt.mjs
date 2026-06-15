#!/usr/bin/env node
/** Fail if legacy .env.txt reappears (use .env + secrets/.local only). */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { LEGACY_ENV_FILES } from "./lib/env-secret-map.mjs";

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const found = LEGACY_ENV_FILES.filter((name) => fs.existsSync(path.join(root, name)));

if (found.length) {
  console.error("Legacy env files present (remove after migrate-env-to-sops):");
  for (const f of found) console.error(`  - ${f}`);
  console.error("Run: node scripts/migrate-env-to-sops.mjs");
  process.exit(1);
}

process.exit(0);
