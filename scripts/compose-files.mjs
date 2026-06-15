import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));

export const rootDir = root;

export const COMPOSE_ENV_FILE = path.join(root, "compose.env");

const COMPOSE_DIR = path.join(root, "infra", "compose");

const FILE_BASE = [
  path.join(root, "docker-compose.yml"),
  path.join(COMPOSE_DIR, "docker-compose.prod.yml"),
  path.join(COMPOSE_DIR, "docker-compose.prod.override.yml"),
];

const FILE_MONITORING = path.join(COMPOSE_DIR, "docker-compose.monitoring.yml");
const FILE_SIMULATE = path.join(COMPOSE_DIR, "docker-compose.simulate.override.yml");
const FILE_SWARM = path.join(COMPOSE_DIR, "docker-compose.swarm.yml");

/** `docker compose --env-file compose.env -f ...` prefix (no subcommand). */
export function composeCmd(extraFiles = []) {
  const files = [...FILE_BASE, ...extraFiles];
  return [
    "compose",
    "--env-file",
    COMPOSE_ENV_FILE,
    ...files.flatMap((f) => ["-f", f]),
  ];
}

export const COMPOSE_BASE = composeCmd();
export const COMPOSE_SIMULATE = composeCmd([FILE_SIMULATE]);
export const COMPOSE_SWARM = composeCmd([FILE_SWARM]);

/** Absolute paths to all compose files in a stack (for `docker stack deploy --compose-file`). */
export function composeFilePaths(extraFiles = []) {
  const files = [...FILE_BASE, ...extraFiles];
  return files;
}

export const COMPOSE_FILE_PATHS_MONITORING = composeFilePaths([FILE_MONITORING]);
export const COMPOSE_FILE_PATHS_SWARM = composeFilePaths([FILE_SWARM]);
