#!/usr/bin/env node
/**
 * One-time (or repeat-safe) migration:
 *   .env / .env.txt / .env.txt.bak  →  secrets/.local/
 *   →  secrets/encrypted/*.enc.yaml (SOPS)
 *   scrub inline secrets from .env
 *   delete legacy .env.txt*
 *
 * Usage:
 *   node scripts/migrate-env-to-sops.mjs [--dry-run] [--keep-txt] [--skip-encrypt]
 */
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import {
  ENV_FILE_POINTERS,
  ENV_TO_LOCAL,
  INLINE_SECRET_KEYS,
  LEGACY_ENV_FILES,
  PLAINTEXT_STAGING_NAMES,
  extractAlchemyKeyFromUrl,
  extractQuicknodeToken,
} from "./lib/env-secret-map.mjs";

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const envPath = path.join(root, ".env");
const localDir = path.join(root, "secrets", ".local");
const encDir = path.join(root, "secrets", "encrypted");
const secretsDir = path.join(root, "secrets");

const dryRun = process.argv.includes("--dry-run");
const keepTxt = process.argv.includes("--keep-txt");
const skipEncrypt = process.argv.includes("--skip-encrypt");

function parseEnv(text) {
  const map = new Map();
  const lines = [];
  for (const line of text.split(/\r?\n/)) {
    lines.push(line);
    const t = line.trim();
    if (!t || t.startsWith("#")) continue;
    const eq = t.indexOf("=");
    if (eq < 1) continue;
    map.set(t.slice(0, eq).trim(), t.slice(eq + 1).trim());
  }
  return { map, lines };
}

function readEnvFile(filePath) {
  if (!fs.existsSync(filePath)) return new Map();
  return parseEnv(fs.readFileSync(filePath, "utf8")).map;
}

function mergeEnvMaps(...maps) {
  const out = new Map();
  for (const m of maps) {
    for (const [k, v] of m) out.set(k, v);
  }
  return out;
}

function isPlaceholder(value) {
  const v = (value || "").trim();
  if (!v) return true;
  return (
    v.startsWith("your_") ||
    v.startsWith("YOUR_") ||
    v === "changeme" ||
    v === "CHANGEME" ||
    v.includes("example.com")
  );
}

function writeLocalFile(name, content) {
  const dest = path.join(localDir, name);
  const body = content.endsWith("\n") ? content : `${content}\n`;
  if (dryRun) {
    console.log(`  [dry-run] would write secrets/.local/${name}`);
    return true;
  }
  fs.mkdirSync(localDir, { mode: 0o700, recursive: true });
  fs.writeFileSync(dest, body, { mode: 0o600 });
  console.log(`  wrote secrets/.local/${name}`);
  return true;
}

function migrateKeysToLocal(merged) {
  let n = 0;
  for (const [envKey, localName] of ENV_TO_LOCAL) {
    const value = (merged.get(envKey) || "").trim();
    if (!value || isPlaceholder(value)) continue;
    const existing = fs.existsSync(path.join(localDir, localName))
      ? fs.readFileSync(path.join(localDir, localName), "utf8").trim()
      : "";
    if (existing && !existing.startsWith("#")) continue;
    if (writeLocalFile(localName, value)) n++;
  }

  let alchemy = (merged.get("ALCHEMY_KEY") || "").trim();
  if (!alchemy) {
    alchemy =
      extractAlchemyKeyFromUrl(merged.get("SOLANA_RPC_URL") || "") ||
      extractAlchemyKeyFromUrl(merged.get("ALCHEMY_RPC") || "");
  }
  if (alchemy && !isPlaceholder(alchemy)) {
    const p = path.join(localDir, "alchemy_api_key");
    if (!fs.existsSync(p) || !fs.readFileSync(p, "utf8").trim()) {
      if (writeLocalFile("alchemy_api_key", alchemy)) n++;
    }
  }

  const qn = extractQuicknodeToken(merged.get("QUICKNODE_RPC") || "");
  if (qn) {
    const p = path.join(localDir, "quicknode_rpc_token");
    if (!fs.existsSync(p) || !fs.readFileSync(p, "utf8").trim()) {
      if (writeLocalFile("quicknode_rpc_token", qn)) n++;
    }
  }

  return n;
}

function scrubEnvFile() {
  if (!fs.existsSync(envPath)) return false;
  let text = fs.readFileSync(envPath, "utf8");
  let changed = false;

  for (const key of INLINE_SECRET_KEYS) {
    const re = new RegExp(`^${key}=.*$`, "m");
    if (re.test(text)) {
      text = text.replace(re, `${key}=`);
      changed = true;
    }
  }

  const alchemyKey = fs.existsSync(path.join(localDir, "alchemy_api_key"))
    ? fs.readFileSync(path.join(localDir, "alchemy_api_key"), "utf8").trim()
    : "";
  if (alchemyKey) {
    const rpcPatterns = [
      ["SOLANA_RPC_URL", "https://solana-mainnet.g.alchemy.com/v2/"],
      ["SOLANA_RPC_URL_FAST", "https://solana-mainnet.g.alchemy.com/v2/"],
      ["SOLANA_RPC_WS_URL", "wss://solana-mainnet.g.alchemy.com/v2/"],
      ["ALCHEMY_RPC", "https://base-mainnet.g.alchemy.com/v2/"],
    ];
    for (const [k, stub] of rpcPatterns) {
      const re = new RegExp(`^${k}=.*$`, "m");
      if (re.test(text) && text.match(re)?.[0]?.includes(alchemyKey)) {
        text = text.replace(re, `${k}=${stub}`);
        changed = true;
      }
    }
    if (/^ALCHEMY_KEY=.+$/m.test(text)) {
      text = text.replace(/^ALCHEMY_KEY=.*$/m, "ALCHEMY_KEY=");
      changed = true;
    }
  }

  const qnToken = fs.existsSync(path.join(localDir, "quicknode_rpc_token"))
    ? fs.readFileSync(path.join(localDir, "quicknode_rpc_token"), "utf8").trim()
    : "";
  if (qnToken && /^QUICKNODE_RPC=.*$/m.test(text)) {
    text = text.replace(
      /^QUICKNODE_RPC=.*$/m,
      "QUICKNODE_RPC=https://responsive-rough-shadow.base-mainnet.quiknode.pro/",
    );
    changed = true;
  }

  for (const [key, pointer] of ENV_FILE_POINTERS) {
    const re = new RegExp(`^${key}=.*$`, "m");
    const line = `${key}=${pointer}`;
    if (re.test(text)) {
      text = text.replace(re, line);
      changed = true;
    } else if (!text.includes(`${key}=`)) {
      text = `${text.replace(/\s*$/, "")}\n${line}\n`;
      changed = true;
    }
  }

  if (!/^SECRETS_ENCRYPTION=/m.test(text)) {
    text = `${text.replace(/\s*$/, "")}\nSECRETS_ENCRYPTION=sops\n`;
    changed = true;
  } else if (!/^SECRETS_ENCRYPTION=sops/m.test(text)) {
    text = text.replace(/^SECRETS_ENCRYPTION=.*$/m, "SECRETS_ENCRYPTION=sops");
    changed = true;
  }

  if (!dryRun && changed) {
    fs.writeFileSync(envPath, text.endsWith("\n") ? text : `${text}\n`, "utf8");
    console.log("  scrubbed inline secrets from .env (use secrets/.local + encrypted/)");
  } else if (dryRun && changed) {
    console.log("  [dry-run] would scrub .env and set *_FILE → secrets/.local/");
  }
  return changed;
}

function deleteLegacyEnvFiles() {
  let removed = 0;
  for (const name of LEGACY_ENV_FILES) {
    const p = path.join(root, name);
    if (!fs.existsSync(p)) continue;
    if (dryRun) {
      console.log(`  [dry-run] would delete ${name}`);
      removed++;
      continue;
    }
    fs.unlinkSync(p);
    console.log(`  removed ${name}`);
    removed++;
  }
  return removed;
}

function runLegacyTxtMerge() {
  const txt = path.join(root, ".env.txt");
  if (!fs.existsSync(txt)) return;
  console.log("=== merging .env.txt into .env (legacy) ===");
  const r = spawnSync("node", [path.join(root, "scripts", "migrate-env-txt.mjs"), ...(keepTxt ? ["--keep-txt"] : [])], {
    cwd: root,
    stdio: "inherit",
    shell: false,
  });
  if ((r.status ?? 1) !== 0 && !dryRun) {
    console.warn("  migrate-env-txt failed — continuing with existing .env");
  }
}

function syncAndEncrypt() {
  if (dryRun) {
    console.log("  [dry-run] would run sync-secrets-local + secrets:encrypt");
    return true;
  }
  const sync = spawnSync(
    "node",
    [path.join(root, "scripts", "sync-secrets-local.mjs"), "--no-migrate-env"],
    { cwd: root, stdio: "inherit", shell: false },
  );
  if ((sync.status ?? 1) !== 0) return false;

  if (skipEncrypt) {
    console.log("  --skip-encrypt: skipped SOPS encrypt");
    return true;
  }

  const enc = spawnSync("node", [path.join(root, "scripts", "ensure-encrypted-secrets.mjs"), "--encrypt"], {
    cwd: root,
    stdio: "inherit",
    shell: false,
  });
  return (enc.status ?? 1) === 0;
}

function pruneStagingPlaintext() {
  let n = 0;
  for (const name of PLAINTEXT_STAGING_NAMES) {
    const p = path.join(secretsDir, name);
    if (!fs.existsSync(p) || !fs.statSync(p).isFile()) continue;
    if (dryRun) {
      console.log(`  [dry-run] would remove secrets/${name}`);
      n++;
      continue;
    }
    fs.unlinkSync(p);
    console.log(`  removed staging secrets/${name}`);
    n++;
  }
  return n;
}

function syncComposeEnv() {
  if (dryRun) {
    console.log("  [dry-run] would run sync:compose-env");
    return;
  }
  const r = spawnSync("node", [path.join(root, "scripts", "sync-compose-env.mjs")], {
    cwd: root,
    stdio: "inherit",
    shell: false,
  });
  if ((r.status ?? 1) !== 0) {
    console.warn("  sync:compose-env failed (non-fatal)");
  }
}

console.log("=== migrate-env-to-sops ===\n");

if (!fs.existsSync(envPath)) {
  console.error("Missing .env — copy from .env.example first.");
  process.exit(1);
}

if (!fs.existsSync(path.join(root, ".sops.yaml"))) {
  console.error("Missing .sops.yaml — copy from .sops.yaml.example and set your age recipient.");
  process.exit(1);
}

runLegacyTxtMerge();

const merged = mergeEnvMaps(
  readEnvFile(envPath),
  readEnvFile(path.join(root, ".env.txt")),
  readEnvFile(path.join(root, ".env.txt.bak")),
);

console.log("\n=== secrets/.local ===");
const written = migrateKeysToLocal(merged);
console.log(`  ${written} secret file(s) prepared in secrets/.local/\n`);

if (!syncAndEncrypt()) {
  console.error("\nEncrypt step failed. Fix SOPS age key (secrets/.local/sops_age_key) and re-run:");
  console.error("  node scripts/migrate-env-to-sops.mjs");
  process.exit(1);
}

console.log("\n=== scrub .env ===");
scrubEnvFile();

console.log("\n=== remove legacy env dumps ===");
const deleted = deleteLegacyEnvFiles();

console.log("\n=== prune staging plaintext (secrets/*, not encrypted/) ===");
pruneStagingPlaintext();

console.log("\n=== compose.env (non-secret vars only) ===");
syncComposeEnv();

console.log("\nDone.");
console.log("  • Plaintext dev: secrets/.local/ (gitignored)");
console.log("  • Encrypted:     secrets/encrypted/*.enc.yaml (commit for team)");
console.log("  • Config only:   .env + auto compose.env");
if (!dryRun) {
  console.log("\nInstall pre-commit: pip install pre-commit && pre-commit install");
  console.log("Optional team sync: Doppler or Infisical → inject into secrets/.local/ (see secrets/README.md)");
}

if (dryRun) console.log("\n(dry-run — no files written, no encrypt)");
