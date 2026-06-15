# Requirements layout

| File | Purpose |
|------|---------|
| `requirements/base.txt` | Production runtime deps |
| `requirements/ml.txt` | Optional ML (`numpy`, `pandas`, `scikit-learn`) |
| `requirements/dev.txt` | Dev tools (`pytest`, `ruff`, `mypy`, `black`) |
| `requirements.txt` | Prod compile input (`-r requirements/base.txt`) |
| `requirements-dev.txt` | Dev compile input (base + ml + dev) |
| `requirements.lock` | Pinned prod install (commit after changes) |
| `requirements-dev.lock` | Pinned dev install |

## Lock / install (uv)

```bash
pip install uv
npm run deps:lock          # compile both locks
uv pip sync requirements.lock
uv pip sync requirements-dev.lock
```

Or:

```bash
uv pip compile requirements.txt -o requirements.lock
uv pip compile requirements-dev.txt -o requirements-dev.lock
```

Docker builder stage uses `requirements.lock` via `uv pip install --system`.
