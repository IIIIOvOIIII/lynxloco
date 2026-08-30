Rollback triggers:
1. Gateway restart fails or RPC status does not recover within the maintenance window.
2. The gateway binds without an acceptable authentication boundary.
3. The Miloco plugin stops loading after the bind change.
4. Miloco backend health becomes unavailable and does not recover after one service restart.

Rollback plan:
1. Restore `/root/.openclaw/openclaw.json` from the CO-specific backup captured before mutation.
2. Restart or reinstall the OpenClaw gateway service using the root systemd user environment (`XDG_RUNTIME_DIR=/run/user/0`) so the previous loopback bind takes effect.
3. Verify `openclaw gateway status --require-rpc` reports read probe OK.
4. Verify `ss -ltnp` no longer shows a wildcard `18789` listener and loopback access to `127.0.0.1:18789` works.
5. Verify `openclaw plugins inspect miloco-openclaw-plugin` still reports `Status: loaded` and `allowConversationAccess: true`.
6. Verify Miloco backend health at `http://127.0.0.1:1810/health` remains OK.
