# Phase 3: Contract audit & secure deploy

## Prerequisites

- **Base EVM deployer key** (separate from Solana trading key): `DEPLOYER_PRIVATE_KEY=0x...`
- **Owner** = Gnosis Safe (preferred): `GNOSIS_SAFE_ADDRESS=0x...`
- Optional timelock: `TIMELOCK_ADDRESS=0x...` (transfer ownership after deploy)
- `BASESCAN_API_KEY` for verification
- No `PRIVATE_KEY` / `PRIVATE_KEY_CEX_DEX` in environment (Solana keys stay in SOPS)

## 1. Full audit

```bash
npm install
npm run audit:all
```

Runs: `hardhat compile` → `solhint` → `slither` (if installed).

## 2. Secure deploy

```bash
npm run compile
npm run deploy:secure
```

Uses Hardhat with `DEPLOYER_PRIVATE_KEY` (not Ledger).

## 3. Verify on Basescan (already deployed)

```env
FLASH_LOAN_CONTRACT=0xYourDeployedRegistry
BASESCAN_API_KEY=your_key
GNOSIS_SAFE_ADDRESS=0xOwnerUsedAtDeploy
DEPLOY_REGISTRY_VERSION=solana-arb-bot-monitor-1
```

```bash
npm run verify:base
npm run verify:base:force   # re-verify if needed
```

Testnet:

```bash
bash scripts/deploy-secure.sh base-sepolia
```

## 4. Ownership (Gnosis Safe + timelock)

`ArbMonitorRegistry` uses **OpenZeppelin `Ownable2Step`**.

| Step | Action |
|------|--------|
| Deploy | Set `GNOSIS_SAFE_ADDRESS` as `initialOwner` in Ignition |
| Verify | Basescan: owner = Safe multisig |
| Timelock | `transferOwnership(timelock)` from Safe → timelock `acceptOwnership()` |
| Hardening | Remove deployer EOA as Safe owner if it was only used for bootstrap |

## Env reference

```env
DEPLOYER_PRIVATE_KEY=0xBaseEvmDeployerOnly
GNOSIS_SAFE_ADDRESS=0xYourSafe
TIMELOCK_ADDRESS=0xOptionalTimelock
BASESCAN_API_KEY=your_key
DEPLOY_REGISTRY_VERSION=solana-arb-bot-monitor-1
```

Solana trading uses `SIGNER_TYPE=hot` + SOPS — see [SIGNING.md](SIGNING.md).
