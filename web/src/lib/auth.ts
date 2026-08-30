export interface DashboardUser {
  id: string;
  username: string;
  displayName: string;
  role: "admin";
  enabled: boolean;
  createdAt: number;
  updatedAt: number;
  lastLoginAt: number | null;
}

export interface AuthStatus {
  needsSetup: boolean;
  authenticated: boolean;
  user: DashboardUser | null;
  csrfToken: string | null;
}

export type AuthView = "setup" | "login" | "dashboard";

export function chooseAuthView(status: AuthStatus): AuthView {
  if (status.needsSetup) return "setup";
  if (!status.authenticated || !status.user) return "login";
  return "dashboard";
}

export type PasswordValidationError = "passwordTooShort" | "passwordMismatch";

export function validatePasswordPair(
  password: string,
  confirm: string,
): PasswordValidationError | null {
  if (password.length < 8) return "passwordTooShort";
  if (password !== confirm) return "passwordMismatch";
  return null;
}

export function enabledAdminCount(users: DashboardUser[]): number {
  return users.filter((user) => user.enabled && user.role === "admin").length;
}

export function canDeleteUser(
  user: DashboardUser,
  currentUserId: string | null,
  users: DashboardUser[],
): boolean {
  if (user.id === currentUserId) return false;
  if (user.enabled && user.role === "admin" && enabledAdminCount(users) <= 1) return false;
  return true;
}

export function canDisableUser(user: DashboardUser, users: DashboardUser[]): boolean {
  if (!user.enabled) return true;
  if (user.role === "admin" && enabledAdminCount(users) <= 1) return false;
  return true;
}
