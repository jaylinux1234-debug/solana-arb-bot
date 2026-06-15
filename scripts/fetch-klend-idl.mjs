#!/usr/bin/env node
/**
 * One-time KLend IDL fetch → idls/klend.json
 * Primary: klend repo target/idl; fallback: klend-sdk src/idl mirror.
 */
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const outDir = path.join(root, "idls");
const outFile = path.join(outDir, "klend.json");

const sources = [
  "https://raw.githubusercontent.com/Kamino-Finance/klend/main/target/idl/klend.json",
  "https://raw.githubusercontent.com/Kamino-Finance/klend/master/target/idl/klend.json",
  "https://raw.githubusercontent.com/Kamino-Finance/klend-sdk/master/src/idl/klend.json",
];

async function fetchIdl() {
  await mkdir(outDir, { recursive: true });
  let lastErr = null;
  for (const url of sources) {
    try {
      const res = await fetch(url, { signal: AbortSignal.timeout(30_000) });
      if (!res.ok) {
        lastErr = new Error(`HTTP ${res.status} for ${url}`);
        continue;
      }
      const text = await res.text();
      JSON.parse(text);
      await writeFile(outFile, text, "utf8");
      console.log(`KLend IDL saved → ${outFile} (${text.length} bytes, ${url})`);
      return;
    } catch (err) {
      lastErr = err;
    }
  }
  throw lastErr ?? new Error("Failed to fetch KLend IDL");
}

fetchIdl().catch((err) => {
  console.error("fetch-klend-idl failed:", err?.message ?? err);
  process.exit(1);
});
