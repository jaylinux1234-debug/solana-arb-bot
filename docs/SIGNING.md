# Signing & monitor isolation

## Production policy (Solana)

- **`SIGNER_TYPE=hot` only** when `APP_ENV=production`
- **No** inline `PRIVATE_KEY` / `PRIVATE_KEY_CEX_DEX` in production environment variables
- Load the Solana key from **SOPS-encrypted** `secrets/encrypted/private_key.enc.yaml` via Docker `PRIVATE_KEY_FILE=/run/secrets/private_key`
- **`ALLOW_HOT_KEY_IN_PROD=0`** — keys must come from secret files, not env
- **Ledger removed** — `LEDGER_SIGN_URL` and `ENABLE_LEDGER_BRIDGE` must be unset/false

## Setup

```bash
npm run secrets:init
npm run secrets:encrypt      # SOPS encrypt private_key + API keys
npm run sync:compose-env
npm run setup:install
```

Set `WALLET_PUBKEY` to the public key matching your SOPS `private_key` file.

## Docker monitor

The production `monitor` service:

- Runs **non-root** with read-only rootfs + tmpfs
- Mounts encrypted secrets via Docker secrets (decrypted at entrypoint with age/SOPS)
- **No USB / privileged** mode (hardware wallet passthrough removed)

## Base contract deploy (optional Phase 3)

Solana signing and Base EVM deploy use **different keys**:

```bash
export DEPLOYER_PRIVATE_KEY=0x...   # Base EVM deployer only — never commit
export GNOSIS_SAFE_ADDRESS=0x...
export BASESCAN_API_KEY=...
npm run deploy:secure
```

Do not put `DEPLOYER_PRIVATE_KEY` in the same file as your Solana `private_key`.

## Air-gapped monitor (optional hardening)

1. Dedicated machine for `monitor` with minimal attack surface
2. RPC egress only (or internal gateway)
3. Full-disk encryption; no browser on the bot OS user
4. Rotate SOPS age keys on compromise

See `secrets/README.md` for encryption at rest.
