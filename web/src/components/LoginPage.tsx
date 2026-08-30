import { useState, type FormEvent, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { ApiError } from "@/api/client";
import { login } from "@/api/auth";
import type { AuthStatus } from "@/lib/auth";

export function LoginPage({
  onDone,
  initialError = null,
}: {
  onDone: (status: AuthStatus) => void;
  initialError?: string | null;
}) {
  const { t } = useTranslation();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(initialError);
  const [submitting, setSubmitting] = useState(false);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      onDone(await login({ username, password }));
    } catch (err) {
      setError(err instanceof ApiError && err.status === 401 ? t("auth.invalidLogin") : t("auth.statusFailed"));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <AuthPageShell title={t("auth.loginTitle")} subtitle={t("auth.loginSubtitle")}>
      <form className="space-y-4" onSubmit={submit}>
        <AuthField label={t("auth.username")} value={username} onChange={setUsername} autoComplete="username" />
        <AuthField label={t("auth.password")} value={password} onChange={setPassword} type="password" autoComplete="current-password" />
        {error && <p className="text-caption text-error" role="alert">{error}</p>}
        <button type="submit" disabled={submitting} className="w-full rounded-md bg-brand-primary px-4 py-2 text-white disabled:opacity-60">
          {t("auth.login")}
        </button>
      </form>
    </AuthPageShell>
  );
}

export function AuthPageShell({ title, subtitle, children }: { title: string; subtitle: string; children: ReactNode }) {
  return (
    <main className="min-h-screen grid place-items-center bg-bg-primary px-4 text-text-primary">
      <section className="w-full max-w-md rounded-xl border border-border bg-bg-secondary p-6 shadow-sm">
        <h1 className="text-title">{title}</h1>
        <p className="mt-2 text-body text-text-secondary">{subtitle}</p>
        <div className="mt-6">{children}</div>
      </section>
    </main>
  );
}

export function AuthField({
  label,
  value,
  onChange,
  type = "text",
  autoComplete,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  type?: "text" | "password";
  autoComplete: string;
}) {
  return (
    <label className="block text-caption text-text-secondary">
      {label}
      <input
        required
        type={type}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        autoComplete={autoComplete}
        className="mt-1 w-full rounded-md border border-border bg-bg-primary px-3 py-2 text-body text-text-primary outline-none focus:border-brand-primary"
      />
    </label>
  );
}
