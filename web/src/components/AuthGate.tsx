import { useEffect, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { getAuthStatus, logout } from "@/api/auth";
import { subscribeSessionExpired } from "@/api/client";
import { chooseAuthView, type AuthStatus } from "@/lib/auth";
import { LoginPage } from "./LoginPage";
import { SetupAdminPage } from "./SetupAdminPage";

export function AuthGate({
  children,
}: {
  children: (auth: AuthStatus, onLogout: () => Promise<void>) => ReactNode;
}) {
  const { t } = useTranslation();
  const [status, setStatus] = useState<AuthStatus | null>(null);
  const [statusFailed, setStatusFailed] = useState(false);

  useEffect(() => {
    let active = true;
    const unsubscribe = subscribeSessionExpired(() => {
      if (active) {
        setStatusFailed(false);
        setStatus({ needsSetup: false, authenticated: false, user: null, csrfToken: null });
      }
    });
    getAuthStatus()
      .then((next) => {
        if (active) {
          setStatusFailed(false);
          setStatus(next);
        }
      })
      .catch(() => {
        if (active) {
          setStatusFailed(true);
          setStatus({ needsSetup: false, authenticated: false, user: null, csrfToken: null });
        }
      });
    return () => {
      active = false;
      unsubscribe();
    };
  }, []);

  if (!status) {
    return <div className="h-screen grid place-items-center text-text-tertiary">{t("auth.checking")}</div>;
  }

  const view = chooseAuthView(status);
  if (view === "setup") return <SetupAdminPage onDone={setStatus} />;
  if (view === "login") {
    return <LoginPage onDone={setStatus} initialError={statusFailed ? t("auth.statusFailed") : null} />;
  }

  return <>{children(status, async () => {
    await logout();
    setStatus(await getAuthStatus());
  })}</>;
}
