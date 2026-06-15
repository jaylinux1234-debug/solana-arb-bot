# Encrypted secrets (SOPS / age)

Docker prod mounts files here as `/run/secrets/*` (see `infra/compose/docker-compose.prod.yml`).

## Create encrypted files

```bash
cp .sops.yaml.example .sops.yaml   # set your age recipient
# Plaintext sources live in ../ (e.g. private_key, jupiter_api_key) — never commit filled plaintext
bash scripts/encrypt-secrets.sh
```

Expected outputs:

| Mount name | Encrypted file |
|------------|----------------|
| `private_key` | `private_key.enc.yaml` |
| `private_key_cex_dex` | `private_key_cex_dex.enc.yaml` |
| `jupiter_api_key` | `jupiter_api_key.enc.yaml` |
| `openai_api_key` | `openai_api_key.enc.yaml` |
| `backpack_secret` | `backpack_secret.enc.yaml` |
| `helius_api_key` | `helius_api_key.enc.yaml` |
| `oneinch_api_key` | `oneinch_api_key.txt.enc.yaml` |
| `cow_api_key` | `cow_api_key.txt.enc.yaml` |
| `pagerduty_routing_key` | `pagerduty_routing_key.txt.enc.yaml` |

## Host decrypt (alternative)

```bash
bash scripts/decrypt-secrets.sh   # writes plaintext to ../ for local dev
```

## In-container decrypt

Set in `.env`:

```env
SECRETS_ENCRYPTION=sops
SOPS_AGE_KEY_FILE=secrets/age.identity
```

Rebuild the monitor image after Dockerfile changes (includes `sops` CLI).
