#!/usr/bin/env node
/** Remove secrets/encrypted/*.enc.yaml directories mistaken for files (Windows mkdir bug). */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const encDir = path.join(
  path.dirname(path.dirname(fileURLToPath(import.meta.url))),
  "secrets",
  "encrypted",
);

if (!fs.existsSync(encDir)) process.exit(0);

let removed = 0;
for (const name of fs.readdirSync(encDir)) {
  if (!name.endsWith(".enc.yaml")) continue;
  const p = path.join(encDir, name);
  if (fs.statSync(p).isDirectory()) {
    fs.rmSync(p, { recursive: true, force: true });
    console.warn(`Removed invalid directory: secrets/encrypted/${name}`);
    removed++;
  }
}
if (removed === 0) console.log("No invalid encrypted secret directories found.");
process.exit(0);
