import { spawnSync } from "node:child_process";

type JsonRecord = Record<string, unknown>;

export interface SharedConfigMutation {
  patch: JsonRecord;
  ensureAgent?: { webhookUrl: string; authBearer: string };
}

const PYTHON_TRANSACTION = String.raw`
import fcntl, json, os, sys, tempfile, time

def is_record(value):
    return isinstance(value, dict)

def deep_merge(target, source):
    for key, value in source.items():
        if is_record(value) and is_record(target.get(key)):
            merged = dict(target[key])
            deep_merge(merged, value)
            target[key] = merged
        else:
            target[key] = value

request = json.load(sys.stdin)
path = request["path"]
parent = os.path.dirname(path)
os.makedirs(parent, exist_ok=True)
lock_fd = os.open(path + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
try:
    os.fchmod(lock_fd, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    try:
        with open(path, encoding="utf-8") as handle:
            existing_text = handle.read()
            parsed = json.loads(existing_text)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        existing_text = None
        parsed = {}
    raw = dict(parsed) if is_record(parsed) else {}
    deep_merge(raw, request["patch"])
    ensure_agent = request.get("ensure_agent")
    if ensure_agent is not None:
        agent = raw.get("agent")
        agent = dict(agent) if is_record(agent) else {}
        if not isinstance(agent.get("webhook_url"), str) or not agent["webhook_url"]:
            agent["webhook_url"] = ensure_agent["webhook_url"]
        agent["auth_bearer"] = ensure_agent["auth_bearer"]
        raw["agent"] = agent
    serialized = json.dumps(raw, ensure_ascii=False, indent=2) + "\n"
    if serialized != existing_text:
        fd, tmp = tempfile.mkstemp(dir=parent, prefix=f".{os.path.basename(path)}.", suffix=".tmp")
        os.fchmod(fd, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            hold_ms = request.get("test_hold_temp_ms", 0)
            if hold_ms:
                time.sleep(hold_ms / 1000)
            if request.get("test_fail_after_temp"):
                raise OSError("test atomic write failure")
            os.replace(tmp, path)
            os.chmod(path, 0o600)
            directory_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    if os.stat(path).st_mode & 0o777 != 0o600:
        if request.get("test_fail_chmod"):
            raise OSError("test chmod failure")
        os.chmod(path, 0o600)
    sys.stdout.write(json.dumps({"raw": raw}, ensure_ascii=False))
finally:
    fcntl.flock(lock_fd, fcntl.LOCK_UN)
    os.close(lock_fd)
`;

function testHookValue(name: string): number | boolean | undefined {
  if (process.env.NODE_ENV !== "test") return undefined;
  if (name === "test_hold_temp_ms") {
    const value = Number.parseInt(
      process.env.MILOCO_SHARED_CONFIG_TEST_HOLD_TEMP_MS ?? "",
      10,
    );
    return Number.isSafeInteger(value) && value > 0 ? value : undefined;
  }
  if (name === "test_fail_after_temp") {
    return process.env.MILOCO_SHARED_CONFIG_TEST_FAIL_AFTER_TEMP === "1" || undefined;
  }
  return process.env.MILOCO_SHARED_CONFIG_TEST_FAIL_CHMOD === "1" || undefined;
}

function pythonCommand(): string {
  return process.env.MILOCO_PYTHON_BIN || "python3";
}

export function transactSharedConfig(
  path: string,
  mutation: SharedConfigMutation,
): JsonRecord {
  const request = JSON.stringify({
    path,
    patch: mutation.patch,
    ensure_agent: mutation.ensureAgent && {
      webhook_url: mutation.ensureAgent.webhookUrl,
      auth_bearer: mutation.ensureAgent.authBearer,
    },
    test_hold_temp_ms: testHookValue("test_hold_temp_ms"),
    test_fail_after_temp: testHookValue("test_fail_after_temp"),
    test_fail_chmod: testHookValue("test_fail_chmod"),
  });
  const result = spawnSync(pythonCommand(), ["-c", PYTHON_TRANSACTION], {
    encoding: "utf8",
    input: request,
    maxBuffer: 8 * 1024 * 1024,
    windowsHide: true,
  });
  if (result.error || result.status !== 0) {
    throw new Error("Miloco shared config transaction failed");
  }
  try {
    const response: unknown = JSON.parse(result.stdout);
    if (
      typeof response !== "object" ||
      response === null ||
      Array.isArray(response) ||
      !("raw" in response) ||
      typeof response.raw !== "object" ||
      response.raw === null ||
      Array.isArray(response.raw)
    ) {
      throw new Error("invalid response");
    }
    return response.raw as JsonRecord;
  } catch {
    throw new Error("Miloco shared config transaction failed");
  }
}
