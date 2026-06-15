/**
 * Demo: Alchemy Base WS — eth_subscribe alchemy_minedTransactions
 *
 * Prerequisites:
 *   npm install -g wscat
 *
 * Copy/paste shell (uses ALCHEMY_WS from env when set):
 *   node demo-scripts.js
 *
 * Or run wscat directly:
 *   node demo-scripts.js --run
 */

import { spawn } from "node:child_process";

const ALCHEMY_WS =
  process.env.ALCHEMY_WS?.trim() ||
  process.env.ALCHEMY_WS_URL?.trim() ||
  "wss://base-mainnet.g.alchemy.com/v2/nUYFobQcZy7bFuS8wNqIh";

const subscribePayload = {
  jsonrpc: "2.0",
  method: "eth_subscribe",
  params: [
    "alchemy_minedTransactions",
    {
      addresses: [
        {
          to: "0x9f3ce0ad29b767d809642a53c2bccc9a130659d7",
          from: "0x228f108fd09450d083bb33fe0cc50ae449bc7e11",
        },
        { to: "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48" },
      ],
      includeRemoved: false,
      hashesOnly: true,
    },
  ],
  id: 1,
};

/** Exact wscat invocation (bash). */
export const WSCAT_ALCHEMY_MINED_TX = `wscat -c ${ALCHEMY_WS} \\
      -w 60 \\
      -x '${JSON.stringify(subscribePayload)}'`;

export function printWscatCommand() {
  console.log(WSCAT_ALCHEMY_MINED_TX);
}

export function runWscat() {
  const payload = JSON.stringify(subscribePayload);
  const child = spawn(
    "wscat",
    ["-c", ALCHEMY_WS, "-w", "60", "-x", payload],
    { stdio: "inherit", shell: process.platform === "win32" },
  );
  child.on("error", (err) => {
    console.error("Failed to start wscat. Install: npm install -g wscat", err);
    process.exit(1);
  });
  child.on("exit", (code) => process.exit(code ?? 0));
}

if (process.argv.includes("--run")) {
  runWscat();
} else {
  printWscatCommand();
}
