# =============================================================================
# SOLANA ARB BOT - HARDENED PRODUCTION (LEDGER REMOVED)
# Multi-stage build — hot wallet via SOPS only, no USB/libusb
# =============================================================================

FROM python:3.11-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    protobuf-compiler \
    libprotobuf-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml requirements.txt requirements.lock README.md ./

RUN pip install --no-cache-dir uv \
    && uv pip install --system --no-cache-dir -r requirements.lock

RUN pip install --no-cache-dir \
        "phoenix-trade @ git+https://github.com/Ellipsis-Labs/phoenixpy.git@8f148701da06ed908c12bb9014c1aac8a1715c1e" \
        "borsh-construct>=0.1.0" \
        "anchorpy>=0.20.0,<0.22.0" \
    && python -c "\
from types import ModuleType; \
import sys; \
from solders.instruction import Instruction; \
t = ModuleType('solana.transaction'); t.Instruction = Instruction; \
sys.modules['solana.transaction'] = t; \
import solana; solana.transaction = t; \
from phoenix.market import Market; \
print('phoenix-trade ok', Market.__name__)\
"

COPY protos ./protos
COPY idls ./idls
COPY src ./src

RUN mkdir -p src/core/generated \
    && touch src/core/generated/__init__.py \
    && if [ -f protos/geyser.proto ]; then \
         python -m grpc_tools.protoc -Iprotos \
           --python_out=src/core/generated \
           --grpc_python_out=src/core/generated protos/geyser.proto; \
       else echo "compile_geyser_protos: skip (no protos/geyser.proto)"; fi \
    && pip install --no-cache-dir --no-deps . \
    && python -m compileall -q src

# -----------------------------------------------------------------------------
# Runtime: non-root, curl + sops (SECRETS_ENCRYPTION=sops)
# -----------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

ARG SOPS_VERSION=3.9.4

RUN useradd -m -u 1000 -s /usr/sbin/nologin botuser \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        libgomp1 \
    && curl -fsSL "https://github.com/getsops/sops/releases/download/v${SOPS_VERSION}/sops-v${SOPS_VERSION}.linux.amd64" \
      -o /usr/local/bin/sops \
    && chmod 0755 /usr/local/bin/sops \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /app/src ./src
COPY --from=builder /app/idls ./idls

COPY scripts/docker-entrypoint.sh scripts/docker_healthcheck.py scripts/healthcheck.py \
    scripts/daily_funnel.py scripts/show_fill_rate_results.py \
    scripts/next_level_metrics.py scripts/auto_tuner.py \
    scripts/usdc_inventory_sync.py scripts/breakeven_sim.py \
    scripts/singleton_guard.py scripts/validate-secrets.py \
    scripts/v2_withdraw_usdc.py scripts/v2_wallet_balance.py scripts/v2_deposit_usdc_to_backpack.py ./scripts/
RUN chmod +x scripts/docker-entrypoint.sh

RUN mkdir -p logs backtest_results pnl_data \
    && chown -R botuser:botuser /app

USER botuser

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    TEST_MODE=false \
    APP_ENV=production

EXPOSE 8000
EXPOSE 8799

HEALTHCHECK --interval=20s --timeout=8s --start-period=90s --retries=5 \
  CMD curl -f http://127.0.0.1:8000/health || exit 1

ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]
CMD ["python", "-m", "src.v2.main"]
