#!/usr/bin/env node
/**
 * Deduplicate .env in-place — last-value-wins for conflicting keys.
 *
 * Run on VPS: node scripts/dedup-env.mjs [--dry-run]
 *
 * Prints a summary of removed duplicates, then rewrites .env.
 * Blank lines and comments are preserved in their original positions;
 * only the *earlier* definition of a duplicated key is dropped.
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const TARGET = path.join(ROOT, ".env");
const DRY_RUN = process.argv.includes("--dry-run");

if (!fs.existsSync(TARGET)) {
    console.error("ERROR: .env not found at", TARGET);
    process.exit(1);
}

const raw = fs.readFileSync(TARGET, "utf8");
const lines = raw.split(/\r?\n/);

// First pass: find the last-occurrence line index for every key.
/** @type {Map<string, number>} key → last line index (0-based) */
const lastIndex = new Map();
for (let i = 0; i < lines.length; i++) {
    const trimmed = lines[i].trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const eq = trimmed.indexOf("=");
    if (eq < 1) continue;
    const key = trimmed.slice(0, eq).trim();
    lastIndex.set(key, i);
}

// Second pass: drop lines that are NOT the last occurrence of their key.
const removed = [];
const kept = [];

for (let i = 0; i < lines.length; i++) {
    const trimmed = lines[i].trim();
    if (!trimmed || trimmed.startsWith("#")) {
        kept.push(lines[i]);
        continue;
    }
    const eq = trimmed.indexOf("=");
    if (eq < 1) {
        kept.push(lines[i]);
        continue;
    }
    const key = trimmed.slice(0, eq).trim();
    if (lastIndex.get(key) === i) {
        kept.push(lines[i]);
    } else {
        removed.push({ line: i + 1, key, value: trimmed.slice(eq + 1).trim() });
    }
}

if (removed.length === 0) {
    console.log(".env has no duplicate keys — nothing to do.");
    process.exit(0);
}

console.log(`Found ${removed.length} duplicate key(s) to remove (earlier definitions):`);
for (const { line, key, value } of removed) {
    const preview = value.length > 60 ? value.slice(0, 57) + "..." : value;
    console.log(`  line ${line}: ${key}=${preview}`);
}

if (DRY_RUN) {
    console.log("\nDry-run mode — .env NOT modified. Remove --dry-run to apply.");
    process.exit(0);
}

// Write deduplicated output, preserving trailing newline behaviour.
const output = kept.join("\n") + (raw.endsWith("\n") ? "\n" : "");
fs.writeFileSync(TARGET, output, "utf8");
console.log(`\n.env rewritten — ${removed.length} duplicate(s) removed.`);
