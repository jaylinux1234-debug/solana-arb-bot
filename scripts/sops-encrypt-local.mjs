#!/usr/bin/env node
/**
 * Encrypt secrets/.local → secrets/encrypted (SOPS).
 * Used by pre-commit and: npm run secrets:encrypt-local
 */
import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));

const sync = spawnSync(
  "node",
  [path.join(root, "scripts", "sync-secrets-local.mjs"), "--no-migrate-env"],
  { cwd: root, stdio: "pipe", shell: false },
);
if ((sync.status ?? 1) !== 0) {
  console.error("sync-secrets-local failed");
  process.exit(1);
}

const enc = spawnSync(
  "node",
  [path.join(root, "scripts", "ensure-encrypted-secrets.mjs"), "--encrypt"],
  { cwd: root, stdio: "inherit", shell: false },
);
process.exit(enc.status ?? 1);
