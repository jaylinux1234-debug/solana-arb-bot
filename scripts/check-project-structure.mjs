#!/usr/bin/env node
/** npm run structure:check — verify infra/ layout */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));

const required = [
  "src/config/settings.py",
  "infra/compose/docker-compose.prod.yml",
  "infra/monitoring/prometheus.yml",
  "docker-compose.yml",
  "Dockerfile",
  "pyproject.toml",
  "package.json",
  ".env.example",
  "secrets/encrypted",
  "scripts/compose-files.mjs",
];

const forbidden = [
  "docker-compose.prod.yml",
  "monitoring/prometheus.yml",
  "restart-monitor.sh",
];

let ok = true;
for (const rel of required) {
  const p = path.join(root, rel);
  if (!fs.existsSync(p)) {
    console.error(`MISSING: ${rel}`);
    ok = false;
  }
}
for (const rel of forbidden) {
  const p = path.join(root, rel);
  if (fs.existsSync(p)) {
    console.error(`LEGACY (move/remove): ${rel}`);
    ok = false;
  }
}

if (ok) {
  console.log("Project structure OK");
  process.exit(0);
}
process.exit(1);
