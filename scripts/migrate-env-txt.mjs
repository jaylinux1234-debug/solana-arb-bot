#!/usr/bin/env node
/**
 * Legacy: merge keys from .env.txt into .env, then remove .env.txt.
 * Prefer: npm run secrets:migrate  (scripts/migrate-env-to-sops.mjs)
 * Usage: node scripts/migrate-env-txt.mjs [--dry-run] [--keep-txt]
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const ENV = path.join(ROOT, ".env");
const TXT = path.join(ROOT, ".env.txt");
const BAK = path.join(ROOT, ".env.txt.bak");

const dryRun = process.argv.includes("--dry-run");
const keepTxt = process.argv.includes("--keep-txt");

function parseKeys(text) {
  const map = new Map();
  for (const line of text.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const eq = trimmed.indexOf("=");
    if (eq < 1) continue;
    map.set(trimmed.slice(0, eq).trim(), trimmed);
  }
  return map;
}

if (!fs.existsSync(TXT)) {
  console.log("No .env.txt — nothing to migrate.");
  process.exit(0);
}

if (!fs.existsSync(ENV)) {
  console.error("Missing .env — copy from .env.example first.");
  process.exit(1);
}

const envText = fs.readFileSync(ENV, "utf8");
const txtText = fs.readFileSync(TXT, "utf8");
const envKeys = parseKeys(envText);
const txtKeys = parseKeys(txtText);

const added = [];
for (const [key, line] of txtKeys) {
  if (!envKeys.has(key)) {
    envKeys.set(key, line);
    added.push(key);
  }
}

if (added.length === 0) {
  console.log("All .env.txt keys already present in .env.");
} else {
  console.log(`Merging ${added.length} key(s) from .env.txt into .env: ${added.join(", ")}`);
  const block = [
    "",
    "# --- migrated from .env.txt ---",
    ...added.map((k) => txtKeys.get(k)),
    "",
  ];
  const merged = `${envText.replace(/\s*$/, "")}${block.join("\n")}`;
  if (!dryRun) fs.writeFileSync(ENV, `${merged}\n`, "utf8");
}

if (!dryRun) {
  fs.copyFileSync(TXT, BAK);
  console.log(`Backed up .env.txt → ${path.basename(BAK)}`);
  if (!keepTxt) {
    fs.unlinkSync(TXT);
    console.log("Removed .env.txt");
  }
}

if (dryRun) {
  console.log("(dry-run — no files written)");
} else {
  console.log("Done. Run: npm run sync:compose-env");
}
