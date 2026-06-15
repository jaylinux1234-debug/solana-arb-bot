# Monitoring & logs

Container name on this project: **`solana-arb-monitor`** (not `aave-flash-monitor`).

## Tail logs continuously

**Compose (recommended — matches prod stack):**

```bash
npm run logs:tail
```

**Direct Docker (same stream, by container name):**

```bash
npm run logs:follow
# equivalent:
docker logs -f solana-arb-monitor
```

**Last N lines then follow:**

```bash
docker logs -f --tail 200 solana-arb-monitor
npm run compose:logs
```

**Simulate stack:**

```bash
npm run logs:tail:simulate
```

**Saved logs** (from `restart-monitor.sh`): `logs/monitor_YYYYMMDD_HHMM.log`

## Structured logs + Loki

Set in `.env` (or monitor `environment`):

```env
LOG_FORMAT=json
LOG_SERVICE_NAME=solana-arb-monitor
```

Promtail ships `./logs/*.log` to Loki when the monitoring overlay is up (`loki-prod`, `promtail-prod`).

In Grafana → Explore → datasource **Loki**:

```logql
{job="solana-bot"} |= "CEX-DEX"
```

## Prometheus + Grafana

| Service | Local | Remote server |
|---------|--------|----------------|
| **Prometheus** | http://localhost:9090 | http://your-server:9090 |
| **Grafana** | http://localhost:3000 | http://your-server:3000 |
| **Bot health** | http://localhost:8000/health | http://your-server:8000/health |
| **Bot metrics** | http://localhost:8000/metrics | http://your-server:8000/metrics |

### Grafana login

Default from compose: **`admin` / `admin`** unless overridden in `.env`:

```env
GRAFANA_ADMIN_USER=admin
GRAFANA_ADMIN_PASSWORD=your_secure_password
```

Dashboard folder: **Solana Arb Bot** (provisioned from `monitoring/grafana/dashboards/`).

### Firewall (VPS)

Only expose what you need:

```bash
# Example: Grafana to your IP only
ufw allow from YOUR_IP to any port 3000
# Prefer SSH tunnel instead of public Grafana:
ssh -L 3000:localhost:3000 user@your-server
# then open http://localhost:3000
```

## Quick status

```bash
npm run health:quick
# or manually:
docker compose --env-file compose.env \
  -f docker-compose.yml -f docker-compose.prod.yml \
  -f docker-compose.prod.override.yml -f docker-compose.monitoring.yml ps
curl -f http://localhost:8000/health || echo "Monitor unhealthy"
```

### Force restart single service

```bash
npm run compose:restart:monitor
# equivalent:
docker compose --env-file compose.env ... restart monitor
```

For rebuild + health wait, use: `npm run compose:prod:restart`

### View recent errors

```bash
npm run logs:errors
# direct container (legacy aave name → use solana-arb-monitor):
docker logs --tail 200 solana-arb-monitor | grep -E "ERROR|Exception|Traceback"
npm run logs:errors:docker
```

## Legacy name mapping

| aave-flashloan-base | solana-arb-bot |
|---------------------|----------------|
| `aave-flash-monitor` | `solana-arb-monitor` |
| `flash-monitor.service` | `solana-arb-monitor.service` or `flash-monitor.service` |
