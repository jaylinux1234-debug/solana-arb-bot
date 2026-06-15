/**
 * Canonical mapping: .env variable → secrets/.local filename.
 * Used by migrate-env-to-sops, sync-secrets-local, and scrubbers.
 */

/** @type {[envKey: string, localName: string][]} */
export const ENV_TO_LOCAL = [
  ["PRIVATE_KEY", "private_key.txt"],
  ["PRIVATE_KEY_CEX_DEX", "private_key_cex_dex"],
  ["JUPITER_API_KEY", "jupiter_api_key"],
  ["OPENAI_API_KEY", "openai_api_key"],
  ["BACKPACK_API_KEY", "backpack_api_key"],
  ["BACKPACK_SECRET", "backpack_secret"],
  ["HELIUS_API_KEY", "helius_api_key"],
  ["ALCHEMY_KEY", "alchemy_api_key"],
  ["ONEINCH_API_KEY", "oneinch_api_key.txt"],
  ["COW_API_KEY", "cow_api_key.txt"],
  ["PAGERDUTY_ROUTING_KEY", "pagerduty_routing_key.txt"],
];

/** Env keys cleared from .env after migration (values live in SOPS / .local). */
export const INLINE_SECRET_KEYS = new Set(ENV_TO_LOCAL.map(([k]) => k));

/** After migration, point loaders at .local (gitignored). */
export const ENV_FILE_POINTERS = [
  ["PRIVATE_KEY_FILE", "secrets/.local/private_key.txt"],
  ["PRIVATE_KEY_CEX_DEX_FILE", "secrets/.local/private_key_cex_dex"],
  ["JUPITER_API_KEY_FILE", "secrets/.local/jupiter_api_key"],
  ["OPENAI_API_KEY_FILE", "secrets/.local/openai_api_key"],
  ["BACKPACK_API_KEY_FILE", "secrets/.local/backpack_api_key"],
  ["BACKPACK_SECRET_FILE", "secrets/.local/backpack_secret"],
  ["HELIUS_API_KEY_FILE", "secrets/.local/helius_api_key"],
];

/** Legacy env files to remove after migration. */
export const LEGACY_ENV_FILES = [".env.txt", ".ENV.txt", ".env.txt.bak"];

/** Plaintext copies under secrets/ (not .local) — removed after encrypt. */
export const PLAINTEXT_STAGING_NAMES = [
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
  "quicknode_rpc_token",
];

export function extractAlchemyKeyFromUrl(url) {
  const m = (url || "").match(/\/v2\/([^/?#\s]+)/i);
  return m?.[1]?.trim() || "";
}

export function extractQuicknodeToken(url) {
  return (url || "").match(/quiknode\.pro\/([^/?#\s]+)/i)?.[1]?.trim() || "";
}
