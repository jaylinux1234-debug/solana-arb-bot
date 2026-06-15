#!/usr/bin/env node
/**
 * Ensure secrets/encrypted/*.enc.yaml exist before compose up (warn or encrypt from .local).
 * Usage: node scripts/ensure-encrypted-secrets.mjs [--encrypt]
 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const encDir = path.join(root, "secrets", "encrypted");

const SECRET_FILES = [
  "private_key",
  "private_key.txt",
  "private_key_cex_dex",
  "jupiter_api_key",
  "openai_api_key",
  "backpack_secret",
  "backpack_api_key",
  "helius_api_key",
  "alchemy_api_key",
  "oneinch_api_key.txt",
  "cow_api_key.txt",
  "pagerduty_routing_key.txt",
];

const REQUIRED = [
  "private_key.enc.yaml",
  "private_key_cex_dex.enc.yaml",
  "jupiter_api_key.enc.yaml",
  "openai_api_key.enc.yaml",
  "backpack_secret.enc.yaml",
  "helius_api_key.enc.yaml",
  "oneinch_api_key.txt.enc.yaml",
  "cow_api_key.txt.enc.yaml",
  "pagerduty_routing_key.txt.enc.yaml",
];

/** Encrypted artifacts optional when no plaintext source exists (Ledger prod). */
const OPTIONAL_ENC = new Set(["private_key_cex_dex.enc.yaml"]);

const LOCAL_ALIASES = {
  "private_key.enc.yaml": ["private_key.txt", "private_key"],
  "private_key_cex_dex.enc.yaml": ["private_key_cex_dex.txt", "private_key_cex_dex"],
  "jupiter_api_key.enc.yaml": ["jupiter_api_key"],
  "openai_api_key.enc.yaml": ["openai_api_key"],
  "backpack_secret.enc.yaml": ["backpack_secret"],
  "helius_api_key.enc.yaml": ["helius_api_key"],
  "oneinch_api_key.txt.enc.yaml": ["oneinch_api_key.txt"],
  "cow_api_key.txt.enc.yaml": ["cow_api_key.txt"],
  "pagerduty_routing_key.txt.enc.yaml": ["pagerduty_routing_key.txt"],
};

function isValidEncFile(name) {
  const p = path.join(encDir, name);
  return fs.existsSync(p) && fs.statSync(p).isFile() && fs.statSync(p).size > 0;
}

function pruneInvalidEncryptedArtifacts() {
  if (!fs.existsSync(encDir)) return;
  for (const name of fs.readdirSync(encDir)) {
    if (!name.endsWith(".enc.yaml")) continue;
    const p = path.join(encDir, name);
    if (fs.statSync(p).isDirectory()) {
      fs.rmSync(p, { recursive: true, force: true });
      console.warn(`Removed invalid directory: secrets/encrypted/${name}`);
    }
  }
}

function syncLocal() {
  const r = spawnSync("node", [path.join(root, "scripts", "sync-secrets-local.mjs")], {
    cwd: root,
    stdio: "inherit",
    shell: false,
  });
  return (r.status ?? 1) === 0;
}

function hasPlaintextSource(encName) {
  const aliases = LOCAL_ALIASES[encName] || [];
  for (const dir of ["secrets", path.join("secrets", ".local")]) {
    for (const alias of aliases) {
      const p = path.join(root, dir, alias);
      if (fs.existsSync(p) && fs.statSync(p).isFile() && fs.statSync(p).size > 0) {
        return true;
      }
    }
  }
  return false;
}

function missing() {
  return REQUIRED.filter((name) => {
    if (isValidEncFile(name)) return false;
    if (OPTIONAL_ENC.has(name) && !hasPlaintextSource(name)) return false;
    return true;
  });
}

function missingOptional() {
  return [...OPTIONAL_ENC].filter((name) => !isValidEncFile(name) && !hasPlaintextSource(name));
}

/** Output filename in secrets/encrypted/ (matches docker-compose + REQUIRED). */
function encOutputName(name) {
  if (name === "private_key.txt") return "private_key.enc.yaml";
  return `${name}.enc.yaml`;
}

function resolveSopsBinary() {
  const bundled = path.join(root, "tools", process.platform === "win32" ? "sops.exe" : "sops");
  if (fs.existsSync(bundled)) return bundled;
  return "sops";
}

function resolveSopsAgeKeyFile() {
  const candidates = [
    process.env.SOPS_AGE_KEY_FILE,
    path.join(root, "secrets", ".local", "sops_age_key"),
    path.join(os.homedir(), ".config", "sops", "age", "keys.txt"),
    path.join(os.homedir(), ".age", "key.txt"),
  ].filter(Boolean);
  for (const p of candidates) {
    if (fs.existsSync(p)) return p;
  }
  return null;
}

function encryptWithSops() {
  const sopsYaml = path.join(root, ".sops.yaml");
  if (!fs.existsSync(sopsYaml)) {
    console.error("Create .sops.yaml from .sops.yaml.example before --encrypt");
    return false;
  }
  const sopsBin = resolveSopsBinary();
  const check = spawnSync(sopsBin, ["--version"], { cwd: root, encoding: "utf8" });
  if ((check.status ?? 1) !== 0) {
    console.error(
      "sops binary not found — install getsops/sops (winget install SecretsOPerationS.SOPS) " +
        "or place tools/sops.exe in the repo",
    );
    return false;
  }
  fs.mkdirSync(encDir, { recursive: true });
  const ageKey = resolveSopsAgeKeyFile();
  const env = { ...process.env };
  if (ageKey) {
    env.SOPS_AGE_KEY_FILE = ageKey;
    console.log(`  using SOPS_AGE_KEY_FILE=${ageKey}`);
  } else {
    console.warn("  WARN: SOPS_AGE_KEY_FILE not found — set age identity for encrypt");
  }
  let ok = true;
  const written = new Set();
  for (const name of SECRET_FILES) {
    const src = path.join(root, "secrets", name);
    if (!fs.existsSync(src) || !fs.statSync(src).isFile()) continue;
    if (fs.statSync(src).size === 0) continue;
    const encName = encOutputName(name);
    if (written.has(encName)) continue;
    const dst = path.join(encDir, encName);
    const override = path.posix.join("secrets", "encrypted", encName);
    const r = spawnSync(
      sopsBin,
      [
        "--encrypt",
        "--filename-override",
        override,
        "--input-type",
        "binary",
        "--output-type",
        "binary",
        src,
      ],
      { cwd: root, encoding: "buffer", maxBuffer: 10 * 1024 * 1024, env },
    );
    if ((r.status ?? 1) !== 0) {
      const err = (r.stderr || Buffer.alloc(0)).toString("utf8").trim();
      console.error(`sops encrypt failed for secrets/${name}${err ? `: ${err}` : ""}`);
      ok = false;
      continue;
    }
    fs.writeFileSync(dst, r.stdout);
    written.add(encName);
    console.log(`  encrypted secrets/${name} → secrets/encrypted/${encName}`);
  }
  return ok;
}

pruneInvalidEncryptedArtifacts();
syncLocal();

const wantEncrypt = process.argv.includes("--encrypt");
const miss = missing();
const localDir = path.join(root, "secrets", ".local");

if (miss.length === 0 && !wantEncrypt) {
  const optional = missingOptional();
  if (optional.length) {
    console.log(`  optional (no source, ledger OK): ${optional.join(", ")}`);
  }
  console.log("Encrypted secrets OK");
  process.exit(0);
}

if (miss.length > 0) {
  console.warn(`Missing ${miss.length} encrypted secret file(s) in secrets/encrypted/:`);
  for (const name of miss) console.warn(`  - ${name}`);

  if (fs.existsSync(localDir)) {
    for (const name of miss) {
      const aliases = LOCAL_ALIASES[name] || [];
      const found = aliases.find((a) => {
        const p = path.join(localDir, a);
        return fs.existsSync(p) && fs.statSync(p).isFile() && fs.statSync(p).size > 0;
      });
      if (found) console.warn(`  source available: secrets/.local/${found}`);
    }
  }
}

if (!wantEncrypt) {
  console.warn("\nRun: npm run secrets:encrypt");
  console.warn("  (compose will use plaintext overlay from secrets/.local when encrypted files are absent)");
  process.exit(miss.length > 0 ? 1 : 0);
}

if (encryptWithSops()) {
  const still = missing();
  if (still.length === 0) {
    const optional = missingOptional();
    if (optional.length) {
      console.log(`  optional (no source, ledger OK): ${optional.join(", ")}`);
    }
    console.log("Encrypted secrets OK");
    process.exit(0);
  }
  console.error(`Still missing required encrypted secrets: ${still.join(", ")}`);
  process.exit(1);
}

if (process.platform === "win32") {
  console.error("sops encrypt failed — install sops and configure .sops.yaml + age key");
  process.exit(1);
}

const r = spawnSync("bash", [path.join(root, "scripts", "encrypt-secrets.sh")], {
  cwd: root,
  stdio: "inherit",
  shell: false,
});
process.exit(r.status ?? 1);