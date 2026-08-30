import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

describe("Vite development authentication boundary", () => {
  it("does not proxy browser requests with a service token", () => {
    const config = fs.readFileSync(path.join(webRoot, "vite.config.ts"), "utf-8");

    expect(config).not.toContain("readBackendToken");
    expect(config).not.toContain("DEV_TOKEN");
    expect(config).not.toContain("attachAuth");
    expect(config).not.toContain("configure: attachAuth");
    expect(config).toContain('host: "127.0.0.1"');
    expect(config).toContain('allowedHosts: ["localhost", "127.0.0.1"]');
  });

  it("keeps the retired dev-server command out of package scripts", () => {
    const packageJson = JSON.parse(
      fs.readFileSync(path.join(webRoot, "package.json"), "utf-8"),
    ) as { scripts: Record<string, string> };

    expect(packageJson.scripts.dev).toBeUndefined();
  });
});
