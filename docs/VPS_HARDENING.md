# VPS Hardening Guide (Solana Arb Bot)

Target host: `167.233.116.94`

This guide is mapped to this repo's current production stack and is designed to reduce compromise risk without changing strategy logic.

## 1) What Is Exposed Right Now

From `docker-compose.yml`, these host ports are published:

- `8000` bot health
- `8799` webhook
- `9091` bot metrics
- `9090` Prometheus
- `3000` Grafana

If these are open to the internet, the attack surface is larger than necessary.

## 2) Immediate Hardening Actions (Host)

Run on VPS as root once.

```bash
apt-get update
apt-get install -y ufw fail2ban

# Default deny inbound, allow outbound
ufw default deny incoming
ufw default allow outgoing

# SSH (replace 22 if you changed SSH port)
ufw allow 22/tcp

# Optional: if webhook must be public
# ufw allow 8799/tcp

# Never expose Grafana/Prometheus/metrics directly unless strictly required
# Use SSH tunnel instead.

ufw --force enable
systemctl enable --now fail2ban
```

## 3) SSH Hardening

Edit `/etc/ssh/sshd_config`:

```text
PermitRootLogin no
PasswordAuthentication no
PubkeyAuthentication yes
MaxAuthTries 3
LoginGraceTime 20
```

Then validate and restart:

```bash
sshd -t
systemctl restart ssh
```

Important: create and test a non-root sudo user with key auth before disabling root login.

## 4) Run Bot With Secure Compose Override

Use this repo file:

- `infra/compose/docker-compose.security.override.yml`

Start stack with security override:

```bash
docker compose \
  -f docker-compose.yml \
  -f infra/compose/docker-compose.security.override.yml \
  up -d
```

This override binds service ports to localhost by default and adds `no-new-privileges`.

## 5) Access Observability Safely (SSH Tunnel)

From your local machine:

```bash
ssh -L 3000:127.0.0.1:3000 -L 9090:127.0.0.1:9090 root@167.233.116.94
```

Then open:

- `http://127.0.0.1:3000` (Grafana)
- `http://127.0.0.1:9090` (Prometheus)

## 6) Secret and Key Hygiene

- Keep encrypted secrets in `secrets/encrypted` only.
- Keep decrypted runtime secrets only in `secrets/.local` on trusted host.
- Enforce filesystem permissions:

```bash
chmod 700 secrets/.local || true
chmod 600 secrets/.local/* || true
```

- Rotate API keys after any suspected incident.

## 7) Docker Daemon Risk Controls

- Do not expose Docker remote API on TCP.
- Restrict access to `/var/run/docker.sock` to trusted admins only.
- Keep host and engine patched.

## 8) Ops Verification Checklist

```bash
# Listening ports
ss -tulpen

# Firewall status
ufw status verbose

# Fail2ban status
fail2ban-client status

# Container health
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'

# Recent auth failures
journalctl -u ssh -n 100 --no-pager
```

## 9) Recommended Order

1. Create non-root deploy user with sudo and SSH keys.
2. Apply SSH hardening and test new login.
3. Enable UFW + fail2ban.
4. Redeploy with security override compose file.
5. Use SSH tunnel for Grafana/Prometheus.
6. Review exposed ports weekly.
