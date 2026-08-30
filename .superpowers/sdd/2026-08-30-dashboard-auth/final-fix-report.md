# Dashboard auth final-review fix report

## Scope and result

Final-review findings were fixed in one local implementation wave from base
`87f631169a79e39dc3faf26b2855012b2a9380d2`. No push, deployment, production
access, credential retrieval, or external mutation was performed.

### Stable per-session CSRF

- Replaced status-driven CSRF rotation with a deterministic per-session CSRF
  value derived from the opaque HttpOnly session token.
- The raw CSRF value is never stored. SQLite continues to store only its
  SHA-256 hash, and request validation continues to compare hashes in constant
  time.
- Repeated `/api/auth/status` and `/api/auth/me` calls now recover the same CSRF
  value, so a second tab or concurrent status refresh cannot invalidate a token
  already held by another tab or in-flight write.
- Existing hashed-session lookup, seven-day expiry, cookie name, and Bearer
  service-token compatibility remain unchanged.

### Public auth abuse controls

- Added request bounds: username 128 characters, password and confirmation 256
  characters, and display name 256 characters.
- Registered the existing unified request-validation handler so rejected input
  values are removed from validation responses and logs.
- Added a concurrency-safe five-minute login-attempt window with independent
  per-account and per-source limits. The structure is memory-bounded, uses the
  direct request source, and blocks before database lookup or Argon2 verification.
- Throttled attempts return the same generic authentication failure as unknown,
  disabled, or wrong-password identities.
- Completed setup is checked before password validation/hashing, while the
  transactional first-admin recheck remains the concurrency authority.

### Frontend session-expiry transition

- Centralized HTTP 401 handling in the dashboard API client.
- Any 401 clears in-memory CSRF state and notifies the mounted `AuthGate`, which
  unmounts the dashboard and returns to login.
- This covers ordinary session expiry/revocation and the next protected reload
  after current-user password change or self-disable.

### Nearby low-cost findings

- Authenticated browser logout now requires the existing CSRF guard; anonymous
  or already-expired logout remains idempotent.
- Router tests now distinguish current-user deletion protection from the
  last-admin guard.
- Added router assertions for successful login cookie/identity and old/new
  password semantics after password change.

## TDD evidence

### RED

Command:

```text
cd backend && uv run pytest miloco/tests/auth/test_auth_router.py miloco/tests/auth/test_auth_service.py -q
```

Observed before production edits: 6 failures and 13 passes. Failures proved:

- oversized public credentials reached login and returned 401 instead of 422;
- authenticated logout without CSRF returned 200 instead of 403;
- a status/me refresh invalidated the original CSRF and the following write
  returned 403;
- completed setup invoked password hashing before returning conflict;
- no per-account throttle existed;
- no per-source throttle existed.

Command:

```text
cd web && npm test -- auth-client.test.ts
```

Observed before production edits: 1 failure and 4 passes. The new test failed
because no centralized session-expiry subscriber existed.

### GREEN

- Focused backend service/router: 19 passed.
- Backend auth suite: 39 passed.
- Related SSE/camera protected-media suites: 63 passed.
- Frontend auth/users boundary set: 14 passed.
- Frontend typecheck: passed.
- Frontend production build: passed.
- Scoped Ruff: passed.
- Full local release gate `./scripts/local-ci.sh --tests`: all 6 gate items
  passed, including the full backend gate and 187 Hermes tests with 2 expected
  skips.
- `git diff --check`: passed.

## Files changed

- `backend/miloco/src/miloco/auth/service.py`
- `backend/miloco/src/miloco/auth/schema.py`
- `backend/miloco/src/miloco/auth/dependencies.py`
- `backend/miloco/src/miloco/main.py`
- `backend/miloco/src/miloco/middleware/exception_handler.py`
- `backend/miloco/tests/auth/test_auth_service.py`
- `backend/miloco/tests/auth/test_auth_router.py`
- `web/src/api/client.ts`
- `web/src/components/AuthGate.tsx`
- `web/tests/auth-client.test.ts`

## Limitations and release boundary

- Login attempt state is intentionally process-local and bounded. The current
  Miloco single-process application receives the control; a future multi-worker
  topology should move the same windows to shared storage before enabling more
  than one application worker.
- The frontend production build retains its pre-existing Vite warning about the
  main bundle exceeding 500 kB; this fix did not change that boundary.
- Production remains unmodified. Deployment still requires the controller's
  approved CO/PAM flow and a pre-deploy data backup.
