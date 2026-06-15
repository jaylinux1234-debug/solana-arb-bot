import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { composeCmd } from "./compose-files.mjs";

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const encDir = path.join(root, "secrets", "encrypted");
const PLAINTEXT_OVERLAY = path.join(root, "infra", "compose", "docker-compose.plaintext-secrets.yml");

const OPTIONAL_ENC = new Set(["private_key_cex_dex.enc.yaml"]);

const REQUIRED_ENC = [
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

export function isValidEncFile(name) {
  const p = path.join(encDir, name);
  if (!fs.existsSync(p)) return false;
  if (fs.statSync(p).isDirectory()) return false;
  return fs.statSync(p).isFile() && fs.statSync(p).size > 0;
}

function hasPlaintextSource(encName) {
  const aliases = {
    "private_key_cex_dex.enc.yaml": ["private_key_cex_dex"],
  }[encName];
  if (!aliases) return true;
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

function missingEncryptedSecrets() {
  return REQUIRED_ENC.filter((name) => {
    if (isValidEncFile(name)) return false;
    if (OPTIONAL_ENC.has(name) && !hasPlaintextSource(name)) return false;
    return true;
  });
}

/** Prod stack compose argv; adds plaintext secrets overlay when encrypted files are absent. */
export function prodComposeCmd(extraFiles = []) {
  const missing = missingEncryptedSecrets();
  const files = [...extraFiles];
  if (missing.length > 0 && fs.existsSync(PLAINTEXT_OVERLAY)) {
    files.push(PLAINTEXT_OVERLAY);
    console.warn(
      `Using plaintext secrets overlay (${missing.length} missing in secrets/encrypted/). Run: npm run secrets:encrypt`,
    );
  }
  return composeCmd(files);
}

export const COMPOSE_MONITORING = prodComposeCmd([
  path.join(root, "infra", "compose", "docker-compose.monitoring.yml"),
]);
