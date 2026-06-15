/**
 * Alchemy Base WS — eth_subscribe alchemy_minedTransactions
 *
 *   node demo-script.js          # print bash wscat command
 *   node demo-script.js --run    # connect via Node WebSocket (Windows-friendly)
 *   node demo-script.js --wscat  # spawn local wscat (Unix / global install)
 */

import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));

const ALCHEMY_WS =
  process.env.ALCHEMY_WS?.trim() ||
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

export const WSCAT_CMD = `wscat -c ${ALCHEMY_WS} \\
      -w 60 \\
      -x '${JSON.stringify(subscribePayload)}'`;

function wscatBin() {
  const win = process.platform === "win32";
  const local = join(__dirname, "node_modules", ".bin", win ? "wscat.cmd" : "wscat");
  if (existsSync(local)) return local;
  return win ? "wscat.cmd" : "wscat";
}

export function printWscatCommand() {
  console.log(WSCAT_CMD);
}

export function runWscat() {
  const payload = JSON.stringify(subscribePayload);
  const child = spawn(wscatBin(), ["-c", ALCHEMY_WS, "-w", "60", "-x", payload], {
    stdio: "inherit",
    shell: false,
  });
  child.on("error", (err) => {
    console.error("wscat failed. Try: node demo-script.js --run", err);
    process.exit(1);
  });
  child.on("exit", (code) => process.exit(code ?? 0));
}

export async function runNativeWs() {
  const ws = new WebSocket(ALCHEMY_WS);
  const waitSec = Number(process.env.DEMO_WS_WAIT_SEC || "60");
  let subscribed = false;
  let sawEvent = false;

  const timeout = setTimeout(() => {
    if (!subscribed) {
      console.error("timeout: subscribe never confirmed");
      ws.close();
      process.exit(1);
      return;
    }
    console.log(
      sawEvent
        ? "done (events received)"
        : `no matching txs in ${waitSec}s (subscribe ok — filter may be quiet)`,
    );
    ws.close();
    process.exit(0);
  }, waitSec * 1000);

  ws.addEventListener("open", () => {
    console.log("connected:", ALCHEMY_WS.replace(/\/v2\/.+/, "/v2/***"));
    ws.send(JSON.stringify(subscribePayload));
    console.log("sent subscribe:", JSON.stringify(subscribePayload));
  });

  ws.addEventListener("message", (ev) => {
    const text = String(ev.data);
    console.log(text);
    try {
      const msg = JSON.parse(text);
      if (msg.id === 1 && msg.result) subscribed = true;
      if (msg.method === "eth_subscription") sawEvent = true;
    } catch {
      /* non-JSON */
    }
  });

  ws.addEventListener("error", (ev) => {
    console.error("websocket error:", ev.message || ev);
    clearTimeout(timeout);
    process.exit(1);
  });

  ws.addEventListener("close", () => {
    clearTimeout(timeout);
    process.exit(subscribed ? 0 : 1);
  });
}

const arg = process.argv[2];
if (arg === "--run") {
  runNativeWs();
} else if (arg === "--wscat") {
  runWscat();
} else {
  printWscatCommand();
}
