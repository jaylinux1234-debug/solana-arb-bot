#!/usr/bin/env node
/**
 * Mirror non-empty secrets/.local/* → secrets/* for Docker mounts and preflight.
 * Optional: --migrate-env pulls BACKPACK_API_KEY / ALCHEMY_KEY from .env into .local once.
 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const localDir = path.join(root, "secrets", ".local");
const secretsDir = path.join(root, "secrets");

/** basename in .local → basename in secrets/ */
const SYNC_MAP = [
  ["private_key.txt", "private_key.txt"],
  ["private_key.txt", "private_key"],
  ["private_key", "private_key"],
  ["private_key_cex_dex", "private_key_cex_dex"],
  ["jupiter_api_key", "jupiter_api_key"],
  ["openai_api_key", "openai_api_key"],
  ["backpack_secret", "backpack_secret"],
  ["backpack_api_key", "backpack_api_key"],
  ["helius_api_key", "helius_api_key"],
  ["alchemy_api_key", "alchemy_api_key"],
  ["quicknode_rpc_token", "quicknode_rpc_token"],
  ["oneinch_api_key.txt", "oneinch_api_key.txt"],
  ["cow_api_key.txt", "cow_api_key.txt"],
  ["pagerduty_routing_key.txt", "pagerduty_routing_key.txt"],
];

function readFileTrim(p) {
  if (!fs.existsSync(p) || !fs.statSync(p).isFile()) return "";
  return fs.readFileSync(p, "utf8").trim();
}

function writeIfChanged(dest, content) {
  const body = content.endsWith("\n") ? content : `${content}\n`;
  if (fs.existsSync(dest) && fs.readFileSync(dest, "utf8") === body) return false;
  fs.mkdirSync(path.dirname(dest), { recursive: true });
  fs.writeFileSync(dest, body, { mode: 0o600 });
  return true;
}

function copyLocalToSecrets() {
  if (!fs.existsSync(localDir)) {
    console.warn("secrets/.local/ not found — run: npm run secrets:init");
    return 0;
  }
  let copied = 0;
  const seen = new Set();
  for (const [srcName, destName] of SYNC_MAP) {
    const key = `${srcName}→${destName}`;
    if (seen.has(key)) continue;
    seen.add(key);
    const src = path.join(localDir, srcName);
    const content = readFileTrim(src);
    if (!content) continue;
    const dest = path.join(secretsDir, destName);
    if (writeIfChanged(dest, content)) {
      console.log(`  synced secrets/.local/${srcName} → secrets/${destName}`);
      copied++;
    }
  }
  return copied;
}

function parseEnv(text) {
  const out = new Map();
  for (const line of text.split(/\r?\n/)) {
    const t = line.trim();
    if (!t || t.startsWith("#")) continue;
    const eq = t.indexOf("=");
    if (eq < 1) continue;
    out.set(t.slice(0, eq).trim(), t.slice(eq + 1).trim());
  }
  return out;
}

function extractAlchemyKeyFromUrl(url) {
  const m = (url || "").match(/\/v2\/([^/?#\s]+)/i);
  return m?.[1]?.trim() || "";
}

function migrateEnvToLocal() {
  const envPath = path.join(root, ".env");
  if (!fs.existsSync(envPath)) return 0;
  fs.mkdirSync(localDir, { recursive: true });
  const env = parseEnv(fs.readFileSync(envPath, "utf8"));
  let n = 0;

  const backpack = (env.get("BACKPACK_API_KEY") || "").trim();
  if (backpack && !readFileTrim(path.join(localDir, "backpack_api_key"))) {
    writeIfChanged(path.join(localDir, "backpack_api_key"), backpack);
    console.log("  migrated BACKPACK_API_KEY → secrets/.local/backpack_api_key");
    n++;
  }

  let alchemy = (env.get("ALCHEMY_KEY") || "").trim();
  if (!alchemy) {
    alchemy =
      extractAlchemyKeyFromUrl(env.get("SOLANA_RPC_URL") || "") ||
      extractAlchemyKeyFromUrl(env.get("ALCHEMY_RPC") || "");
  }
  if (alchemy && !readFileTrim(path.join(localDir, "alchemy_api_key"))) {
    writeIfChanged(path.join(localDir, "alchemy_api_key"), alchemy);
    console.log("  migrated Alchemy key → secrets/.local/alchemy_api_key");
    n++;
  }

  const qn = (env.get("QUICKNODE_RPC") || "").trim();
  const qnToken = qn.match(/quiknode\.pro\/([^/?#\s]+)/i)?.[1]?.trim();
  if (qnToken && !readFileTrim(path.join(localDir, "quicknode_rpc_token"))) {
    writeIfChanged(path.join(localDir, "quicknode_rpc_token"), qnToken);
    console.log("  migrated QuickNode token → secrets/.local/quicknode_rpc_token");
    n++;
  }

  return n;
}

function stripSecretsFromEnv() {
  const envPath = path.join(root, ".env");
  if (!fs.existsSync(envPath)) return false;
  let text = fs.readFileSync(envPath, "utf8");
  let changed = false;

  const alchemyKey = readFileTrim(path.join(localDir, "alchemy_api_key"));
  const qnToken = readFileTrim(path.join(localDir, "quicknode_rpc_token"));

  if (readFileTrim(path.join(localDir, "backpack_api_key"))) {
    const next = text.replace(/^BACKPACK_API_KEY=.*$/m, "BACKPACK_API_KEY=");
    if (next !== text) {
      text = next;
      changed = true;
    }
  }

  if (alchemyKey) {
    const next = text.replace(/^ALCHEMY_KEY=.*$/m, "ALCHEMY_KEY=");
    let t2 = next.replace(
      /^SOLANA_RPC_URL=.*$/m,
      "SOLANA_RPC_URL=https://solana-mainnet.g.alchemy.com/v2/",
    );
    t2 = t2.replace(
      /^SOLANA_RPC_URL_FAST=.*$/m,
      "SOLANA_RPC_URL_FAST=https://solana-mainnet.g.alchemy.com/v2/",
    );
    t2 = t2.replace(
      /^SOLANA_RPC_WS_URL=.*$/m,
      "SOLANA_RPC_WS_URL=wss://solana-mainnet.g.alchemy.com/v2/",
    );
    t2 = t2.replace(
      /^ALCHEMY_RPC=.*$/m,
      "ALCHEMY_RPC=https://base-mainnet.g.alchemy.com/v2/",
    );
    if (t2 !== text) {
      text = t2;
      changed = true;
    }
  }

  if (qnToken) {
    const t2 = text.replace(
      /^QUICKNODE_RPC=.*$/m,
      "QUICKNODE_RPC=https://responsive-rough-shadow.base-mainnet.quiknode.pro/",
    );
    if (t2 !== text) {
      text = t2;
      changed = true;
    }
  }

  if (changed) {
    fs.writeFileSync(envPath, text, "utf8");
    console.log("  scrubbed inline keys from .env (keys now in secrets/.local/)");
  }
  return changed;
}

function syncSopsAgeKey() {
  const dest = path.join(localDir, "sops_age_key");
  if (fs.existsSync(dest) && fs.statSync(dest).isFile() && fs.statSync(dest).size > 0) {
    return false;
  }
  const candidates = [
    process.env.SOPS_AGE_KEY_FILE,
    path.join(os.homedir(), ".config", "sops", "age", "keys.txt"),
    path.join(os.homedir(), ".age", "key.txt"),
  ].filter(Boolean);
  for (const src of candidates) {
    if (!fs.existsSync(src) || !fs.statSync(src).isFile()) continue;
    fs.mkdirSync(localDir, { recursive: true });
    fs.copyFileSync(src, dest);
    try {
      fs.chmodSync(dest, 0o600);
    } catch {
      /* windows */
    }
    console.log("  synced SOPS age key -> secrets/.local/sops_age_key");
    return true;
  }
  console.warn("  WARN: no age key found — place identity at secrets/.local/sops_age_key");
  return false;
}

const migrate = process.argv.includes("--migrate-env") || !process.argv.includes("--no-migrate-env");

function ensureOptionalPlaceholders() {
  fs.mkdirSync(localDir, { recursive: true });
  const cexDex = path.join(localDir, "private_key_cex_dex");
  if (!fs.existsSync(cexDex)) {
    fs.writeFileSync(cexDex, "# optional — Ledger prod (SIGNER_TYPE=ledger)\n", { mode: 0o600 });
    console.log("  created placeholder secrets/.local/private_key_cex_dex");
  }
}

console.log("=== secrets:sync-local ===");
syncSopsAgeKey();
ensureOptionalPlaceholders();
let migrated = 0;
if (migrate) migrated = migrateEnvToLocal();
const copied = copyLocalToSecrets();
if (migrate && migrated > 0) stripSecretsFromEnv();
console.log(`Done (${copied} file(s) synced${migrated ? `, ${migrated} migrated` : ""}).`);
