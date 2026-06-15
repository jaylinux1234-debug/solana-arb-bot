# Secrets (SOPS-only)

Production uses **only** `secrets/encrypted/*.enc.yaml` (SOPS). Local dev uses **`secrets/.local/`** (gitignored). Config (thresholds, RPC hostnames without keys) stays in **`.env`**.

## Layout

| Path | Commit? | Purpose |
|------|---------|---------|
| `encrypted/*.enc.yaml` | **Yes** (team) | SOPS-encrypted blobs — Docker `/run/secrets/*` |
| `.local/` | **No** | Plaintext edit surface for dev |
| `../.env` | **No** | Non-secret settings + `*_FILE` pointers into `.local/` |
| `../compose.env` | **No** | Auto-generated subset for Compose (no API keys) |

## One-time migration (from `.env` / `.env.txt`)

```bash
cp .sops.yaml.example .sops.yaml   # set age recipient
npm run secrets:init
npm run secrets:migrate            # node scripts/migrate-env-to-sops.mjs
```

Dry-run first:

```bash
npm run secrets:migrate:dry
```

This will:

1. Merge and delete **`.env.txt`** / **`.env.txt.bak`**
2. Copy secret values → **`secrets/.local/`**
3. Run **`npm run secrets:encrypt`** (SOPS → `encrypted/`)
4. Scrub inline keys from **`.env`**
5. Regenerate **`compose.env`** (non-secrets only)

## Daily workflow

```bash
# Edit plaintext (never commit)
vim secrets/.local/jupiter_api_key

# Encrypt for prod / git
npm run secrets:encrypt-local
# or: npm run secrets:encrypt
```

### Pre-commit (recommended)

```bash
pip install pre-commit
pre-commit install
```

On commit, changes under `secrets/.local/` re-run SOPS encrypt into `secrets/encrypted/`.

## Optional: Doppler / Infisical (team)

For shared dev secrets without copying files:

1. Create a project (e.g. `solana-arb-bot-dev`).
2. Map secrets to the same names as `secrets/.local/*` files.
3. Inject before run:

```bash
# Doppler
doppler run -- npm run compose:prod:restart

# Infisical
infisical run -- npm run compose:prod:restart
```

Keep **SOPS `encrypted/`** as the source of truth for prod/CI; use Doppler/Infisical only to populate `.local/` on developer machines.

## Files in `.local/`

| File | Env |
|------|-----|
| `private_key.txt` | `PRIVATE_KEY` (SOPS-encrypted for prod; `SIGNER_TYPE=hot`) |
| `private_key_cex_dex` | `PRIVATE_KEY_CEX_DEX` |
| `jupiter_api_key` | `JUPITER_API_KEY` |
| `openai_api_key` | `OPENAI_API_KEY` |
| `backpack_api_key` | `BACKPACK_API_KEY` |
| `backpack_secret` | `BACKPACK_SECRET` |
| `helius_api_key` | `HELIUS_API_KEY` |
| `alchemy_api_key` | `ALCHEMY_KEY` / RPC path token |
| `oneinch_api_key.txt` | `ONEINCH_API_KEY` |
| `cow_api_key.txt` | `COW_API_KEY` |
| `pagerduty_routing_key.txt` | `PAGERDUTY_ROUTING_KEY` |
| `sops_age_key` | Age identity for encrypt/decrypt |

```bash
chmod 700 secrets secrets/.local secrets/encrypted
chmod 600 secrets/.local/* 2>/dev/null || true
```
