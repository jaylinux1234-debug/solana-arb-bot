# Project setup

Path below uses this repo (`solana-arb-bot`). Adjust `cd` if you cloned elsewhere.

## 1. Clone / ensure project is ready

```bash
cd /path/to/solana-arb-bot
git status   # optional: confirm clean tree before prod work
```

Windows (PowerShell):

```powershell
cd C:\Users\you\solana-arb-bot
```

Copy env templates (do **not** commit secrets):

```bash
cp .env.example .env
mkdir -p secrets
npm run secrets:init
npm run go-live:secrets:sh
npm run sync:compose-env
```

Secret templates: `secrets/private_key.txt`, API keys — see [secrets/README.md](../secrets/README.md).  
**Production:** encrypt with SOPS (`npm run secrets:encrypt`); set `SIGNER_TYPE=hot` and `WALLET_PUBKEY`.

## 2. Install dependencies

### Node (Hardhat, compose scripts)

```bash
npm install
```

### Python (bot + monitor)

```bash
python -m venv venv
source venv/bin/activate   # Windows: .\venv\Scripts\activate
pip install uv
npm run deps:lock
uv pip sync requirements-dev.lock
pip install -e ".[dev]"
```

One-liner:

```bash
npm run setup:install
```

Windows:

```powershell
npm run setup:install:ps1
```

### Verify

```bash
npm run compile
npm run test:py
python scripts/validate_go_live_env.py
```

## 3. Hot wallet signing (production)

1. Generate or import a Solana keypair; store in `secrets/` and encrypt with SOPS.
2. Set in `.env`:

```env
SIGNER_TYPE=hot
ALLOW_HOT_KEY_IN_PROD=0
WALLET_PUBKEY=YourBase58Pubkey
PRIVATE_KEY_FILE=secrets/.local/private_key.txt   # local dev; Docker uses /run/secrets/private_key
LEDGER_SIGN_URL=
ENABLE_LEDGER_BRIDGE=false
```

3. Never commit plaintext `private_key.txt`. Run `npm run secrets:encrypt` before prod deploy.

More detail: [SIGNING.md](SIGNING.md).

## 4. Docker + monitoring (optional)

```bash
npm run sync:compose-env
npm run compose:prod:up
```

Health:

```bash
curl http://127.0.0.1:8000/health
npm run health:quick
```

## 5. Go live checklist

- `LIVE_TRADING_CONFIRM=YES`
- `KILL_SWITCH_ON_LOSS=1`
- `python scripts/validate_go_live_env.py`
- [GO_LIVE.md](GO_LIVE.md)
