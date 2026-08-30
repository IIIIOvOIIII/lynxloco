# Dashboard Authentication Design

Status: approved approach B; written specification for user review on 2026-08-30.

## Goal

Miloco should have native dashboard authentication instead of relying on a trusted LAN and an injected service token. After this change, a person opening the dashboard must either create the first administrator or log in with a local dashboard account before they can see or operate the home dashboard.

The requested user-facing behavior is:

- During installation or first use, guide the user to set a username and password.
- Unauthorized dashboard access shows a login/setup page instead of the dashboard.
- After successful login, the user enters the dashboard and sees the current user plus a logout button in the top area.
- A new Users tab lets an administrator add users, delete users, edit user information, and change passwords.

## Current State

Current Miloco has API service-token authentication, but it does not have human dashboard authentication.

The backend API checks `Authorization: Bearer <server.token>` for protected routes. However, the SPA root page injects `server.token` into the returned HTML as `window.__MILOCO_TOKEN__`, and the frontend reads that value and adds it to API calls. Therefore any browser that can load `/` can obtain API authority.

This was acceptable only under the earlier trust model: private single-admin LAN access. It is not sufficient once `server.host=0.0.0.0` exposes the dashboard to a broader network.

## Approved Approach

Use native local dashboard users with server-side sessions:

- Local users are stored in Miloco's SQLite database.
- Passwords are stored only as password hashes.
- Browser dashboard authentication uses an HttpOnly session cookie.
- Machine callers such as CLI, OpenClaw, Hermes, and automation keep using the existing `server.token` Bearer mechanism.
- The dashboard no longer receives `server.token` before login.
- The first administrator is created through a setup flow when no dashboard user exists.

SSO/OIDC and reverse-proxy authentication are intentionally out of scope for this first native implementation.

## Trust Boundary

Miloco will have two separate authentication lanes.

| Lane | Caller | Credential | Purpose |
| --- | --- | --- | --- |
| Dashboard session | Human browser dashboard | HttpOnly session cookie plus CSRF token for writes | Normal web UI access |
| Service token | CLI, OpenClaw, Hermes, scripts, trusted integrations | `Authorization: Bearer <server.token>` | Machine-to-machine access |

These lanes are equivalent only after successful authentication. The service token remains powerful, but it must not be given to unauthenticated browsers.

## Data Model

Add database schema version `3`.

### `dashboard_user`

Stores local dashboard users.

Required fields:

- `id`: stable user id.
- `username`: unique login name, case-insensitive uniqueness.
- `display_name`: optional human display name.
- `password_hash`: password hash string; never plaintext.
- `role`: initially `admin`; retained for future roles.
- `enabled`: boolean.
- `created_at`: Unix ms.
- `updated_at`: Unix ms.
- `last_login_at`: Unix ms or null.

Initial role scope:

- All dashboard users are administrators in this release.
- The `role` field exists to avoid another schema change when finer roles are added later.

### `dashboard_session`

Stores server-side browser sessions.

Required fields:

- `id`: stable session id.
- `user_id`: owner user id.
- `session_hash`: hash of the opaque browser session token.
- `csrf_hash`: hash of the CSRF token associated with this session.
- `created_at`: Unix ms.
- `expires_at`: Unix ms.
- `last_seen_at`: Unix ms.
- `user_agent_hash`: best-effort browser fingerprint hash.
- `client_ip_hint`: optional short non-secret hint for operator diagnostics.

Session tokens and CSRF tokens are never stored in plaintext. Only hashes are persisted.

## Password Policy

Use a mature password hashing library. Preferred implementation is Argon2id via `argon2-cffi`.

If `argon2-cffi` proves unsuitable for the packaging target, the fallback is Python standard-library PBKDF2-HMAC-SHA256 with a per-password random salt and a high iteration count. The fallback must be explicitly documented in code and tests.

Minimum password policy for this release:

- At least 8 characters.
- Username cannot be empty.
- Password and confirmation must match in setup and password-change flows.
- Error messages must not reveal whether a username exists during login.

No password, session token, CSRF token, service token, API key, or RTSP URL may be printed to logs, command output, memory files, docs, screenshots, or git history.

## Backend Components

Add a focused auth package:

- `miloco.auth.schema`: request and response models.
- `miloco.auth.passwords`: password hashing and verification.
- `miloco.auth.repo`: SQLite persistence for users and sessions.
- `miloco.auth.service`: setup, login, logout, session validation, user administration.
- `miloco.auth.router`: HTTP API endpoints.
- `miloco.auth.dependencies`: FastAPI dependencies for dashboard-or-service authentication and CSRF enforcement.

Do not fold this into the existing admin router. Admin API and dashboard auth are related, but they should remain separately testable.

## API Contract

### Public auth endpoints

These endpoints are reachable without an existing session.

#### `GET /api/auth/status`

Returns dashboard authentication state.

Response data:

- `needs_setup`: true when no enabled dashboard user exists.
- `authenticated`: true when the request has a valid dashboard session.
- `user`: current user summary or null.
- `csrf_token`: current CSRF token only when authenticated.

This endpoint must not expose `server.token`.

#### `POST /api/auth/setup`

Creates the first administrator and logs the browser in.

Allowed only when no dashboard user exists. Once any dashboard user exists, the endpoint returns conflict.

Request:

- `username`
- `display_name`
- `password`
- `password_confirm`

Response:

- Sets the session cookie.
- Returns current user summary and CSRF token.

#### `POST /api/auth/login`

Logs in an existing enabled dashboard user.

Request:

- `username`
- `password`

Response:

- Sets the session cookie.
- Returns current user summary and CSRF token.

Failed login returns a generic authentication failure. It must not say whether the username or password was wrong.

#### `POST /api/auth/logout`

Logs out the current dashboard session if present.

Behavior:

- Deletes the server-side session when one exists.
- Clears the session cookie.
- Is idempotent enough that clicking logout twice does not create a noisy error.

### Protected auth endpoint

#### `GET /api/auth/me`

Returns current user summary and CSRF token. Requires a valid dashboard session or service token.

### User management endpoints

All user management endpoints require an authenticated dashboard administrator or the service token.

#### `GET /api/users`

List dashboard users. Response must not include password hashes or session data.

#### `POST /api/users`

Create a new enabled dashboard user.

Request:

- `username`
- `display_name`
- `password`
- `password_confirm`

#### `PATCH /api/users/{user_id}`

Edit user metadata:

- `username`
- `display_name`
- `enabled`

Constraints:

- Cannot disable the last enabled administrator.
- Cannot rename a user to an existing username.

#### `POST /api/users/{user_id}/password`

Change a user's password.

Request:

- `password`
- `password_confirm`

For this first admin-only release, an authenticated admin can reset any user's password. A user self-service password-change screen can later add `current_password` verification.

#### `DELETE /api/users/{user_id}`

Delete a user.

Constraints:

- Cannot delete the last enabled administrator.
- Cannot delete the currently logged-in user in the same browser session.
- Deleting a user invalidates that user's active sessions.

## Route Protection Rules

Introduce a new dependency, conceptually `verify_dashboard_or_service_auth`.

For protected API routes:

- Accept `Authorization: Bearer <server.token>` as before.
- Accept a valid dashboard session cookie.
- Reject unauthenticated requests with 401.

For state-changing API routes called through dashboard sessions:

- Require a valid CSRF token in `X-Miloco-CSRF`.
- Service-token requests do not require CSRF.
- Safe reads such as `GET` do not require CSRF.

Protected route migration should be broad and mechanical: existing routes that currently depend on `verify_token` should move to the new combined dependency unless they are deliberately service-token-only.

## SPA and Static Asset Behavior

The SPA handler must stop injecting `server.token` into `index.html`.

New behavior:

- `/` and `/index.html` return the SPA without service-token injection.
- The SPA loads, calls `/api/auth/status`, and chooses setup, login, or dashboard view.
- Real static assets under `/assets`, `/fonts`, and `vendor` keep existing cache behavior.
- Unknown paths remain 404 unless a future frontend routing design explicitly adds whitelisted SPA routes.

The frontend API client must stop using `window.__MILOCO_TOKEN__` for dashboard API calls. It should use cookie-based requests with `credentials: "same-origin"` and add CSRF headers for modifying requests after login.

## Browser Media, SSE, and WebSocket Behavior

The previous implementation used injected or query tokens in places where browser APIs cannot set an Authorization header. This must be redesigned so authentication does not depend on leaking the long-lived service token to the browser.

Target behavior:

- Same-origin image, video, iframe, SSE, and WebSocket requests should authenticate with the dashboard session cookie when possible.
- WebSocket handlers must accept a valid dashboard session cookie in addition to the existing service token path.
- SSE endpoints should accept a valid dashboard session cookie.
- Query-token fallback must not use the long-lived service token for unauthenticated dashboard users.

If a browser API still requires a URL token after investigation, implement a short-lived media token minted for the current dashboard session. That token should be scoped to media access, expire quickly, and be stored only hashed server-side.

## Frontend UX

### Startup flow

On app load:

1. Call `GET /api/auth/status`.
2. If `needs_setup=true`, render first-admin setup.
3. Else if `authenticated=false`, render login.
4. Else render the existing dashboard shell.

No dashboard data API should be called until the auth status allows it.

### First-admin setup page

Fields:

- Username.
- Display name.
- Password.
- Confirm password.

Behavior:

- Validate required username and password confirmation client-side.
- Submit to `/api/auth/setup`.
- On success, enter dashboard.
- On conflict because setup already exists, refresh auth status and show login.

### Login page

Fields:

- Username.
- Password.

Behavior:

- Submit to `/api/auth/login`.
- On success, enter dashboard.
- On failure, show a generic message such as "用户名或密码不正确".
- Do not keep the password in localStorage or sessionStorage.

### Dashboard user menu

Add a compact user area in the dashboard top bar:

- Show display name or username.
- Show logout button.
- Logout calls `/api/auth/logout`, clears local auth state, and returns to login page.

This is separate from the Xiaomi Home account entry. Xiaomi account binding remains the smart-home provider identity, not the dashboard user identity.

### Users tab

Add a new navigation tab:

- `TabKey`: `users`
- Chinese label: `用户`
- Chinese hint: `管理登录账号`
- English label: `Users`
- English hint: `Manage dashboard accounts`

Required page functions:

- List users.
- Add user.
- Edit username/display name/enabled state.
- Change password.
- Delete user.

Safety UX:

- Disable or reject deleting/disabling the last enabled administrator.
- Disable deleting the current user, or show a clear rejection message.
- Confirm deletion before submitting.
- Mask password inputs.

## Installer Behavior

The installer should include a dashboard administrator step after backend service initialization.

Interactive install:

- If at least one dashboard user exists, show a short "dashboard user already configured" message and skip.
- If no dashboard user exists, ask whether to create the first administrator now.
- Recommended default is to create one immediately.
- Prompt username with visible input.
- Prompt password and confirmation with hidden input.
- Create the user through a local CLI or local backend call that does not place the password in shell history.

Non-interactive install:

- Do not require credentials on the command line.
- If no dashboard user exists, complete the install and print a clear next step: open the dashboard and create the first administrator.
- A future enhancement may add `--dashboard-user` plus `--dashboard-password-stdin`; this is not required for the first implementation.

## CLI Support

Add minimal CLI support for installer and recovery:

- `miloco-cli auth status`
- `miloco-cli auth setup --username <name> --password-stdin`
- `miloco-cli auth reset-password --username <name> --password-stdin`

CLI auth commands use local configuration and service-token access. They must never print passwords or password hashes.

The existing CLI commands for devices, perception, cameras, Home Assistant, rules, schedules, and admin operations continue to use `server.token` and should not be forced through browser sessions.

## Error Handling

Use stable business error codes/messages for:

- Authentication required.
- Invalid login.
- Setup already completed.
- Setup required.
- User not found.
- Username already exists.
- Last administrator cannot be disabled or deleted.
- Current user cannot be deleted.
- CSRF token missing or invalid.
- Session expired.

The frontend should translate these into plain-language messages. Raw backend tracebacks, password hashes, session tokens, CSRF tokens, service tokens, provider API keys, and RTSP URLs must never be surfaced.

## Testing

### Backend tests

Add focused tests for:

- Fresh database has `needs_setup=true`.
- Setup creates the first admin, sets a session cookie, and returns user summary plus CSRF token.
- Setup cannot be run after the first user exists.
- Login success sets a session cookie.
- Login failure returns a generic authentication error and no session cookie.
- Logout invalidates the session and clears the cookie.
- Protected API rejects unauthenticated browser requests.
- Protected API accepts valid dashboard session requests.
- Protected API still accepts valid service-token requests.
- Write requests through dashboard sessions require CSRF.
- Write requests through service token do not require CSRF.
- User list omits password/session data.
- Create user, edit user, change password, and delete user work.
- Last enabled administrator cannot be disabled or deleted.
- Current user cannot be deleted from the same browser session.
- Deleting or disabling a user invalidates their active sessions when appropriate.
- Expired sessions are rejected.
- SPA root no longer contains an injected service token.

### Frontend tests

Add tests for:

- Startup renders setup page when `needs_setup=true`.
- Startup renders login page when unauthenticated.
- Startup renders dashboard only when authenticated.
- Login success enters dashboard.
- Logout returns to login.
- User menu shows current user.
- Users tab is present in desktop and mobile navigation.
- Users page can render list/add/edit/password/delete states.
- API client sends cookie-mode requests and CSRF header for modifying requests.
- Frontend code no longer depends on `window.__MILOCO_TOKEN__` for dashboard access.

### Installer tests

Add tests or smoke coverage for:

- Interactive path detects existing dashboard users and skips.
- Interactive first-admin creation does not place passwords on command line.
- Non-interactive path gives a clear setup instruction instead of failing.

## Deployment and Production Verification

Production deployment to `miloco.esxi` requires an approved CO/PAM change.

Pre-deploy:

- Confirm exact source SHA.
- Back up Miloco runtime data directory before schema migration.
- Verify local tests and web build.

Post-deploy:

- Service health is OK.
- Root dashboard no longer exposes service token in HTML.
- Fresh unauthenticated browser shows setup or login.
- After creating/logging in, dashboard loads normally.
- User menu shows the logged-in dashboard user.
- Logout prevents further dashboard API access.
- Existing CLI/OpenClaw service-token calls still work.
- RTSP live view, Omni model config, Home Assistant page, perception logs, and Users tab are reachable after login.

No production verification may print or store passwords, password hashes, session tokens, CSRF tokens, service tokens, Xiaomi OAuth material, model API keys, RTSP URLs, or camera frames.

## Out of Scope

- Lynx One Login / OIDC integration.
- Reverse-proxy authentication.
- Fine-grained roles beyond admin.
- Password reset email.
- MFA/passkeys.
- Public internet hardening beyond local session auth and CSRF.
- Rotating the existing service token during this feature.
- Changing Xiaomi OAuth binding behavior.
- Changing RTSP camera credential storage.
- Changing Omni model/provider behavior.
- Changing Home Assistant token storage or device-control semantics.

## Acceptance Criteria

The design is complete when:

- An unauthenticated browser cannot access dashboard data by loading `/`.
- A first administrator can be created from a fresh install/setup state.
- A user can log in, use the dashboard, see their user identity, and log out.
- Administrators can add, edit, delete, and change passwords for local dashboard users.
- Existing machine integrations using `server.token` remain compatible.
- The old service-token injection into dashboard HTML is removed.
- Browser media and streaming paths do not require exposing the long-lived service token to unauthenticated browsers.
- Local and production verification records contain no secrets.
