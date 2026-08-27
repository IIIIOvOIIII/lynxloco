import { spawn } from "node:child_process";
import {
  chmodSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

const PYTHON = process.env.MILOCO_PYTHON_BIN || "python3";

const BACKEND_COMPATIBLE_WRITER = String.raw`
import fcntl, json, os, sys, tempfile, time
path = sys.argv[1]
lock_fd = os.open(path + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
try:
    os.fchmod(lock_fd, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        raw = {}
    print("READY", flush=True)
    time.sleep(0.3)
    raw["camera"] = {"rtsp_sources": [{"id": "rtsp:00000000-0000-0000-0000-000000000001", "uri": "rtsp://camera.local/stream1"}]}
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    os.fchmod(fd, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(raw, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
finally:
    fcntl.flock(lock_fd, fcntl.LOCK_UN)
    os.close(lock_fd)
`;

const TEMP_MODE_WATCHER = String.raw`
import os, stat, sys, time
directory, prefix = sys.argv[1:]
deadline = time.monotonic() + 3
while time.monotonic() < deadline:
    for name in os.listdir(directory):
        if name.startswith("." + prefix) and name.endswith(".tmp"):
            print(oct(stat.S_IMODE(os.stat(os.path.join(directory, name)).st_mode)), flush=True)
            sys.exit(0)
    time.sleep(0.002)
sys.exit(2)
`;

function waitForReady(child: ReturnType<typeof spawn>): Promise<void> {
  return new Promise((resolve, reject) => {
    let output = "";
    child.stdout?.on("data", (chunk: Buffer) => {
      output += chunk.toString("utf8");
      if (output.includes("READY")) resolve();
    });
    child.once("error", reject);
    child.once("exit", (code) => {
      if (!output.includes("READY")) reject(new Error(`writer exited: ${code}`));
    });
  });
}

function waitForExit(child: ReturnType<typeof spawn>): Promise<string> {
  return new Promise((resolve, reject) => {
    let output = "";
    child.stdout?.on("data", (chunk: Buffer) => (output += chunk.toString("utf8")));
    child.once("error", reject);
    child.once("exit", (code) => {
      if (code === 0) resolve(output.trim());
      else reject(new Error(`writer exited: ${code}`));
    });
  });
}

describe("shared config security transaction", () => {
  let originalHome: string | undefined;
  let originalPython: string | undefined;
  let originalUmask: number;
  let home: string;
  let configPath: string;

  beforeEach(() => {
    originalHome = process.env.MILOCO_HOME;
    originalPython = process.env.MILOCO_PYTHON_BIN;
    originalUmask = process.umask();
    home = mkdtempSync(path.join(tmpdir(), "miloco-secure-config-"));
    configPath = path.join(home, "config.json");
    process.env.MILOCO_HOME = home;
  });

  afterEach(() => {
    process.umask(originalUmask);
    delete process.env.MILOCO_SHARED_CONFIG_TEST_HOLD_TEMP_MS;
    delete process.env.MILOCO_SHARED_CONFIG_TEST_FAIL_AFTER_TEMP;
    if (originalHome === undefined) delete process.env.MILOCO_HOME;
    else process.env.MILOCO_HOME = originalHome;
    if (originalPython === undefined) delete process.env.MILOCO_PYTHON_BIN;
    else process.env.MILOCO_PYTHON_BIN = originalPython;
    chmodSync(home, 0o700);
    rmSync(home, { recursive: true, force: true });
  });

  it("waits for the backend flock writer and preserves both agent and RTSP updates", async () => {
    writeFileSync(configPath, JSON.stringify({ server: { token: "existing" } }));
    const backend = spawn(PYTHON, ["-c", BACKEND_COMPATIBLE_WRITER, configPath], {
      stdio: ["ignore", "pipe", "pipe"],
    });
    const backendFinished = waitForExit(backend);
    await waitForReady(backend);

    const { updateSharedConfig } = await import("../src/miloco/config.js");
    updateSharedConfig({ agent: { webhook_url: "http://plugin.local/webhook" } });
    await backendFinished;

    const onDisk = JSON.parse(readFileSync(configPath, "utf8"));
    expect(onDisk.agent.webhook_url).toBe("http://plugin.local/webhook");
    expect(onDisk.camera.rtsp_sources).toHaveLength(1);
    expect(onDisk.camera.rtsp_sources[0].id).toBe(
      "rtsp:00000000-0000-0000-0000-000000000001",
    );
  });

  it.each([0o022, 0o027])(
    "uses owner-only temporary and final config files under umask %o",
    async (umask) => {
      process.umask(umask);
      process.env.MILOCO_SHARED_CONFIG_TEST_HOLD_TEMP_MS = "200";
      const watcher = spawn(PYTHON, ["-c", TEMP_MODE_WATCHER, home, "config.json"], {
        stdio: ["ignore", "pipe", "pipe"],
      });
      const watched = waitForExit(watcher);

      const { updateSharedConfig } = await import("../src/miloco/config.js");
      updateSharedConfig({ agent: { webhook_url: "http://plugin.local/webhook" } });

      expect(await watched).toBe("0o600");
      expect(statSync(configPath).mode & 0o777).toBe(0o600);
    },
  );

  it("retains the original config and removes temporary files after a write failure", async () => {
    const original = JSON.stringify({ camera: { rtsp_sources: [{ id: "rtsp:keep" }] } });
    writeFileSync(configPath, original);
    process.env.MILOCO_SHARED_CONFIG_TEST_FAIL_AFTER_TEMP = "1";

    const { updateSharedConfig } = await import("../src/miloco/config.js");
    expect(() => updateSharedConfig({ agent: { webhook_url: "http://plugin.local/webhook" } })).toThrow();

    expect(readFileSync(configPath, "utf8")).toBe(original);
    expect(readdirSync(home).filter((name) => name.endsWith(".tmp"))).toEqual([]);
  });

  it("fails without Python and leaves the existing config untouched", async () => {
    const original = JSON.stringify({ camera: { rtsp_sources: [{ id: "rtsp:keep" }] } });
    writeFileSync(configPath, original);
    process.env.MILOCO_PYTHON_BIN = path.join(home, "missing-python");

    const { updateSharedConfig } = await import("../src/miloco/config.js");
    expect(() => updateSharedConfig({ agent: { webhook_url: "http://plugin.local/webhook" } })).toThrow(
      "Miloco shared config transaction failed",
    );

    expect(readFileSync(configPath, "utf8")).toBe(original);
  });
});
