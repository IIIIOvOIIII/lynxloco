import { apiFetch, setCsrfToken } from "./client";
import type { AuthStatus, DashboardUser } from "@/lib/auth";

interface Normal<T> {
  code: number;
  message: string;
  data: T;
}

type RawUser = Record<string, unknown>;
type RawStatus = Record<string, unknown>;

export interface SetupAdminInput {
  username: string;
  displayName: string;
  password: string;
  passwordConfirm: string;
}

export interface LoginInput {
  username: string;
  password: string;
}

export interface DashboardUserInput extends SetupAdminInput {}

export interface DashboardUserUpdate {
  username?: string;
  displayName?: string;
  enabled?: boolean;
}

function mapUser(raw: RawUser): DashboardUser {
  return {
    id: String(raw.id),
    username: String(raw.username),
    displayName: String(raw.display_name ?? ""),
    role: "admin",
    enabled: Boolean(raw.enabled),
    createdAt: Number(raw.created_at),
    updatedAt: Number(raw.updated_at),
    lastLoginAt: raw.last_login_at == null ? null : Number(raw.last_login_at),
  };
}

function mapStatus(raw: RawStatus): AuthStatus {
  const userRaw = raw.user;
  const status: AuthStatus = {
    needsSetup: Boolean(raw.needs_setup),
    authenticated: Boolean(raw.authenticated),
    user: userRaw && typeof userRaw === "object" ? mapUser(userRaw as RawUser) : null,
    csrfToken: typeof raw.csrf_token === "string" ? raw.csrf_token : null,
  };
  setCsrfToken(status.csrfToken);
  return status;
}

function userPayload(input: SetupAdminInput): Record<string, string> {
  return {
    username: input.username,
    display_name: input.displayName,
    password: input.password,
    password_confirm: input.passwordConfirm,
  };
}

export async function getAuthStatus(): Promise<AuthStatus> {
  const resp = await apiFetch<Normal<RawStatus>>("/api/auth/status");
  return mapStatus(resp.data);
}

export async function setupFirstAdmin(input: SetupAdminInput): Promise<AuthStatus> {
  const resp = await apiFetch<Normal<RawStatus>>("/api/auth/setup", {
    method: "POST",
    body: JSON.stringify(userPayload(input)),
  });
  return mapStatus(resp.data);
}

export async function login(input: LoginInput): Promise<AuthStatus> {
  const resp = await apiFetch<Normal<RawStatus>>("/api/auth/login", {
    method: "POST",
    body: JSON.stringify(input),
  });
  return mapStatus(resp.data);
}

export async function logout(): Promise<void> {
  try {
    await apiFetch<Normal<undefined>>("/api/auth/logout", { method: "POST" });
  } finally {
    setCsrfToken(null);
  }
}

export async function listDashboardUsers(): Promise<DashboardUser[]> {
  const resp = await apiFetch<Normal<{ users?: RawUser[] }>>("/api/users");
  return (resp.data.users ?? []).map(mapUser);
}

export async function createDashboardUser(input: DashboardUserInput): Promise<DashboardUser> {
  const resp = await apiFetch<Normal<RawUser>>("/api/users", {
    method: "POST",
    body: JSON.stringify(userPayload(input)),
  });
  return mapUser(resp.data);
}

export async function updateDashboardUser(
  userId: string,
  input: DashboardUserUpdate,
): Promise<DashboardUser> {
  const body: Record<string, string | boolean> = {};
  if (input.username !== undefined) body.username = input.username;
  if (input.displayName !== undefined) body.display_name = input.displayName;
  if (input.enabled !== undefined) body.enabled = input.enabled;
  const resp = await apiFetch<Normal<RawUser>>(`/api/users/${encodeURIComponent(userId)}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
  return mapUser(resp.data);
}

export async function changeDashboardUserPassword(
  userId: string,
  password: string,
  passwordConfirm: string,
): Promise<DashboardUser> {
  const resp = await apiFetch<Normal<RawUser>>(
    `/api/users/${encodeURIComponent(userId)}/password`,
    {
      method: "POST",
      body: JSON.stringify({ password, password_confirm: passwordConfirm }),
    },
  );
  return mapUser(resp.data);
}

export async function deleteDashboardUser(userId: string): Promise<void> {
  await apiFetch<Normal<undefined>>(`/api/users/${encodeURIComponent(userId)}`, {
    method: "DELETE",
  });
}
