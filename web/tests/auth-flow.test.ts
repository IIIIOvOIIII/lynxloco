import { describe, expect, it } from "vitest";
import { chooseAuthView, validatePasswordPair } from "@/lib/auth";

describe("auth view selection", () => {
  it("shows setup before login when no user exists", () => {
    expect(chooseAuthView({ needsSetup: true, authenticated: false, user: null, csrfToken: null })).toBe("setup");
  });

  it("shows login when setup is complete but user is anonymous", () => {
    expect(chooseAuthView({ needsSetup: false, authenticated: false, user: null, csrfToken: null })).toBe("login");
  });

  it("shows dashboard only after authentication", () => {
    expect(chooseAuthView({
      needsSetup: false,
      authenticated: true,
      csrfToken: "csrf",
      user: {
        id: "u1",
        username: "lynx",
        displayName: "Lynx",
        role: "admin",
        enabled: true,
        createdAt: 1,
        updatedAt: 1,
        lastLoginAt: null,
      },
    })).toBe("dashboard");
  });
});

describe("password validation", () => {
  it("requires matching passwords with at least eight chars", () => {
    expect(validatePasswordPair("1234567", "1234567")).toBe("passwordTooShort");
    expect(validatePasswordPair("12345678", "87654321")).toBe("passwordMismatch");
    expect(validatePasswordPair("12345678", "12345678")).toBe(null);
  });
});
