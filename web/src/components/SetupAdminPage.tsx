import { useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { setupFirstAdmin } from "@/api/auth";
import { validatePasswordPair, type AuthStatus } from "@/lib/auth";
import { AuthField, AuthPageShell } from "./LoginPage";

export function SetupAdminPage({ onDone }: { onDone: (status: AuthStatus) => void }) {
  const { t } = useTranslation();
  const [username, setUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [passwordConfirm, setPasswordConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const validation = validatePasswordPair(password, passwordConfirm);
    if (validation) {
      setError(t(`auth.${validation}`));
      return;
    }
    setError(null);
    setSubmitting(true);
    try {
      onDone(await setupFirstAdmin({ username, displayName, password, passwordConfirm }));
    } catch {
      setError(t("auth.statusFailed"));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <AuthPageShell title={t("auth.setupTitle")} subtitle={t("auth.setupSubtitle")}>
      <form className="space-y-4" onSubmit={submit}>
        <AuthField label={t("auth.username")} value={username} onChange={setUsername} autoComplete="username" />
        <AuthField label={t("auth.displayName")} value={displayName} onChange={setDisplayName} autoComplete="name" />
        <AuthField label={t("auth.password")} value={password} onChange={setPassword} type="password" autoComplete="new-password" />
        <AuthField label={t("auth.passwordConfirm")} value={passwordConfirm} onChange={setPasswordConfirm} type="password" autoComplete="new-password" />
        {error && <p className="text-caption text-error" role="alert">{error}</p>}
        <button type="submit" disabled={submitting} className="w-full rounded-md bg-brand-primary px-4 py-2 text-white disabled:opacity-60">
          {t("auth.createAdmin")}
        </button>
      </form>
    </AuthPageShell>
  );
}
