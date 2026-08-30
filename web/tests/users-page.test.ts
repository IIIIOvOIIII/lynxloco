import { describe, expect, it } from "vitest";
import { canDeleteUser, canDisableUser } from "@/lib/auth";
import type { DashboardUser } from "@/lib/auth";

function user(id: string, enabled = true): DashboardUser {
  return {
    id,
    username: id,
    displayName: id,
    role: "admin",
    enabled,
    createdAt: 1,
    updatedAt: 1,
    lastLoginAt: null,
  };
}

describe("user admin safety rules", () => {
  it("does not allow deleting the current user", () => {
    expect(canDeleteUser(user("u1"), "u1", [user("u1"), user("u2")])).toBe(false);
  });

  it("does not allow deleting or disabling the last enabled admin", () => {
    expect(canDeleteUser(user("u1"), "u2", [user("u1")])).toBe(false);
    expect(canDisableUser(user("u1"), [user("u1")])).toBe(false);
  });

  it("allows changing a non-current admin when another admin remains enabled", () => {
    expect(canDeleteUser(user("u2"), "u1", [user("u1"), user("u2")])).toBe(true);
    expect(canDisableUser(user("u2"), [user("u1"), user("u2")])).toBe(true);
  });
});
