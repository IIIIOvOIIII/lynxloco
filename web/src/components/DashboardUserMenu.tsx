import { useTranslation } from "react-i18next";
import type { AuthStatus } from "@/lib/auth";

export function DashboardUserMenu({
  auth,
  onLogout,
}: {
  auth: AuthStatus;
  onLogout: () => void | Promise<void>;
}) {
  const { t } = useTranslation();
  const label = auth.user?.displayName || auth.user?.username || t("auth.userFallback");
  return (
    <div className="flex items-center gap-2 text-caption text-text-secondary">
      <span className="truncate max-w-[160px]">{label}</span>
      <button type="button" onClick={() => void onLogout()} className="px-2 py-1 rounded-md border border-border hover:border-border-strong">
        {t("auth.logout")}
      </button>
    </div>
  );
}
