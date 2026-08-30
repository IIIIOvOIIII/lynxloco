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
