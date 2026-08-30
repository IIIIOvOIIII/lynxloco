import { useEffect, useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import {
  changeDashboardUserPassword,
  createDashboardUser,
  deleteDashboardUser,
  listDashboardUsers,
  updateDashboardUser,
} from "@/api/auth";
import type { DashboardUser } from "@/lib/auth";
import { canDeleteUser, canDisableUser, validatePasswordPair } from "@/lib/auth";
import { toast } from "./Toast";

type DialogState =
  | { kind: "create" }
  | { kind: "edit"; user: DashboardUser }
  | { kind: "password"; user: DashboardUser }
  | null;

export function UsersPage({ currentUserId }: { currentUserId: string | null }) {
  const { t } = useTranslation();
  const [users, setUsers] = useState<DashboardUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dialog, setDialog] = useState<DialogState>(null);

  const reload = async () => {
    setLoading(true);
    setError(null);
    try {
      setUsers(await listDashboardUsers());
    } catch {
      setError(t("auth.usersLoadFailed"));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void reload();
    // This page owns its initial load; language changes do not need another request.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const setEnabled = async (user: DashboardUser, enabled: boolean) => {
    if (!enabled && !canDisableUser(user, users)) {
      toast(t("auth.lastAdminProtected"), "warn");
      return;
    }
    setBusy(true);
    try {
      await updateDashboardUser(user.id, { enabled });
      await reload();
      toast(t(enabled ? "auth.userEnabled" : "auth.userDisabled"), "ok");
    } catch {
      toast(t("auth.userUpdateFailed"), "warn");
    } finally {
      setBusy(false);
    }
  };

  const remove = async (user: DashboardUser) => {
    if (!canDeleteUser(user, currentUserId, users)) {
      toast(
        user.id === currentUserId ? t("auth.cannotDeleteCurrentUser") : t("auth.lastAdminProtected"),
        "warn",
      );
      return;
    }
    if (!window.confirm(t("auth.deleteUserConfirm", { username: user.username }))) return;
    setBusy(true);
    try {
      await deleteDashboardUser(user.id);
      await reload();
      toast(t("auth.userDeleted"), "ok");
    } catch {
      toast(t("auth.userDeleteFailed"), "warn");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-page-title text-text-primary">{t("auth.usersTitle")}</h1>
          <p className="mt-1 text-body text-text-secondary">{t("auth.usersSubtitle")}</p>
        </div>
        <button
          type="button"
          onClick={() => setDialog({ kind: "create" })}
          disabled={busy}
          className="rounded-md bg-brand-primary px-3 py-2 text-body text-white disabled:opacity-60"
        >
          {t("auth.addUser")}
        </button>
      </div>

      {loading ? (
        <p className="py-8 text-body text-text-tertiary">{t("auth.usersLoading")}</p>
      ) : error ? (
        <div className="rounded-xl border border-error bg-error-bg p-4">
          <p className="text-body text-error" role="alert">{error}</p>
          <button
            type="button"
            onClick={() => void reload()}
            className="mt-3 rounded-md border border-border px-3 py-1.5 text-caption text-text-primary"
          >
            {t("auth.retry")}
          </button>
        </div>
      ) : (
        <div className="overflow-hidden rounded-xl border border-border bg-bg-secondary divide-y divide-border">
          {users.length === 0 ? (
            <p className="p-5 text-body text-text-tertiary">{t("auth.noUsers")}</p>
          ) : (
            users.map((user) => {
              const deleteAllowed = canDeleteUser(user, currentUserId, users);
              const disableAllowed = canDisableUser(user, users);
              return (
                <div key={user.id} className="flex flex-wrap items-center justify-between gap-4 p-4">
                  <div className="min-w-0">
                    <div className="text-title text-text-primary truncate">
                      {user.displayName || user.username}
                      {user.id === currentUserId && (
                        <span className="ml-2 text-caption text-text-tertiary">{t("auth.currentUser")}</span>
                      )}
                    </div>
                    <div className="mt-1 text-caption-mono text-text-tertiary truncate">
                      {user.username} · {user.enabled ? t("auth.enabled") : t("auth.disabled")}
                    </div>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <label className="inline-flex items-center gap-2 text-caption text-text-secondary">
                      <input
                        type="checkbox"
                        checked={user.enabled}
                        disabled={busy || (user.enabled && !disableAllowed)}
                        onChange={(event) => void setEnabled(user, event.target.checked)}
                        aria-label={t("auth.enabledFor", { username: user.username })}
                      />
                      {t("auth.enabled")}
                    </label>
                    <button
                      type="button"
                      onClick={() => setDialog({ kind: "password", user })}
                      disabled={busy}
                      className="rounded-md border border-border px-2.5 py-1.5 text-caption text-text-primary disabled:opacity-50"
                    >
                      {t("auth.changePassword")}
                    </button>
                    <button
                      type="button"
                      onClick={() => setDialog({ kind: "edit", user })}
                      disabled={busy}
                      className="rounded-md border border-border px-2.5 py-1.5 text-caption text-text-primary disabled:opacity-50"
                    >
                      {t("auth.editUser")}
                    </button>
                    <button
                      type="button"
                      onClick={() => void remove(user)}
                      disabled={busy || !deleteAllowed}
                      title={!deleteAllowed ? (user.id === currentUserId ? t("auth.cannotDeleteCurrentUser") : t("auth.lastAdminProtected")) : undefined}
                      className="rounded-md border border-error px-2.5 py-1.5 text-caption text-error disabled:opacity-40"
                    >
                      {t("auth.deleteUser")}
                    </button>
                  </div>
                </div>
              );
            })
          )}
        </div>
      )}

      {dialog && (
        <UserDialog
          state={dialog}
          onClose={() => setDialog(null)}
          onSaved={async (successMessage) => {
            setDialog(null);
            await reload();
            toast(t(successMessage), "ok");
          }}
        />
      )}
    </div>
  );
}

function UserDialog({
  state,
  onClose,
  onSaved,
}: {
  state: Exclude<DialogState, null>;
  onClose: () => void;
  onSaved: (successMessage: string) => Promise<void>;
}) {
  const { t } = useTranslation();
  const isCreate = state.kind === "create";
  const isPassword = state.kind === "password";
  const existingUser = isCreate ? null : state.user;
  const [username, setUsername] = useState(existingUser?.username ?? "");
  const [displayName, setDisplayName] = useState(existingUser?.displayName ?? "");
  const [password, setPassword] = useState("");
  const [passwordConfirm, setPasswordConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (isCreate || isPassword) {
      const validation = validatePasswordPair(password, passwordConfirm);
      if (validation) {
        setError(t(`auth.${validation}`));
        return;
      }
    }
    setError(null);
    setSubmitting(true);
    try {
      if (isCreate) {
        await createDashboardUser({ username, displayName, password, passwordConfirm });
        await onSaved("auth.userCreated");
      } else if (isPassword) {
        await changeDashboardUserPassword(state.user.id, password, passwordConfirm);
        await onSaved("auth.passwordChanged");
      } else {
        await updateDashboardUser(state.user.id, { username, displayName });
        await onSaved("auth.userUpdated");
      }
    } catch {
      setError(t(
        isCreate
          ? "auth.userCreateFailed"
          : isPassword
            ? "auth.passwordChangeFailed"
            : "auth.userUpdateFailed",
      ));
    } finally {
      setSubmitting(false);
    }
  };

  const title = isCreate
    ? t("auth.addUser")
    : isPassword
      ? t("auth.changePassword")
      : t("auth.editUser");

  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 p-4"
      onClick={submitting ? undefined : onClose}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="user-dialog-title"
        className="w-full max-w-md rounded-xl border border-border bg-bg-secondary p-6 shadow-lg"
        onClick={(event) => event.stopPropagation()}
      >
        <h2 id="user-dialog-title" className="text-title text-text-primary">{title}</h2>
        <form className="mt-5 space-y-4" onSubmit={(event) => void submit(event)}>
          {!isPassword && (
            <>
              <UserField label={t("auth.username")} value={username} onChange={setUsername} autoComplete="username" />
              <UserField label={t("auth.displayName")} value={displayName} onChange={setDisplayName} autoComplete="name" />
            </>
          )}
          {(isCreate || isPassword) && (
            <>
              <UserField label={t("auth.password")} value={password} onChange={setPassword} type="password" autoComplete="new-password" />
              <UserField label={t("auth.passwordConfirm")} value={passwordConfirm} onChange={setPasswordConfirm} type="password" autoComplete="new-password" />
            </>
          )}
          {error && <p className="text-caption text-error" role="alert">{error}</p>}
          <div className="flex justify-end gap-2 pt-2">
            <button
              type="button"
              disabled={submitting}
              onClick={onClose}
              className="rounded-md border border-border px-3 py-2 text-body text-text-primary disabled:opacity-50"
            >
              {t("auth.cancel")}
            </button>
            <button
              type="submit"
              disabled={submitting}
              className="rounded-md bg-brand-primary px-3 py-2 text-body text-white disabled:opacity-60"
            >
              {t("auth.save")}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

function UserField({
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
