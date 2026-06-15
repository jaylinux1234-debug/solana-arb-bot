#!/bin/bash
set -euo pipefail
echo "Rotating secrets..."
# Example for private_key
NEW_KEY=$(openssl rand -hex 32)
echo "0x$NEW_KEY" > secrets/private_key.new.txt
mv secrets/private_key.new.txt secrets/private_key.txt
docker secret rm flash_private_key || true
docker secret create flash_private_key secrets/private_key.txt
echo "Restart: docker compose up -d --force-recreate"
# Extend for other keys + notify PagerDuty