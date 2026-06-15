#!/usr/bin/env node
/** Free a TCP port (Windows / Linux / macOS). */
import { execSync } from "node:child_process";

const port = String(process.argv[2] || "8546").trim();

function killWindows() {
  let out = "";
  try {
    out = execSync(`netstat -ano | findstr :${port}`, { encoding: "utf8" });
  } catch {
    return;
  }
  const pids = new Set();
  for (const line of out.split(/\r?\n/)) {
    if (!/LISTENING/i.test(line)) continue;
    const parts = line.trim().split(/\s+/);
    const pid = parts[parts.length - 1];
    if (pid && /^\d+$/.test(pid) && pid !== "0") pids.add(pid);
  }
  for (const pid of pids) {
    try {
      execSync(`taskkill /PID ${pid} /F`, { stdio: "ignore" });
      console.log(`Freed port ${port} (stopped PID ${pid})`);
    } catch {
      /* already gone */
    }
  }
}

function killUnix() {
  try {
    execSync(`lsof -ti :${port} | xargs -r kill -9`, {
      stdio: "ignore",
      shell: true,
    });
    console.log(`Freed port ${port}`);
  } catch {
    /* nothing listening */
  }
}

if (process.platform === "win32") killWindows();
else killUnix();
