Pre-checks:
1. Use the approved PAM scope only for `miloco.esxi` as `root`.
2. Confirm the OpenClaw gateway is installed as the root systemd user service and currently reports healthy RPC status.
3. Inspect only safe gateway configuration fields: current `gateway.bind`, `gateway.port`, gateway auth mode, and whether a token or password is present. Do not print any token, password, SecretRef value, cookie, Miloco API key, Xiaomi token, RTSP URL, or camera data.
4. Confirm port `18789` is currently listening only on loopback before the change and that no other service is using `0.0.0.0:18789`.

Execution steps:
1. Back up `/root/.openclaw/openclaw.json` to `/root/openclaw-bind-<CO>-<timestamp>/openclaw.json.pre-bind` before mutation.
2. Set OpenClaw gateway bind mode to LAN using the supported configuration value `gateway.bind=lan`. This resolves to wildcard listening on `0.0.0.0`; do not set `gateway.bind` directly to the legacy host alias `0.0.0.0`.
3. Leave the existing gateway auth configuration in place. If no gateway token/password/accepted auth mode is present, stop before exposing the service and report the blocker.
4. Refresh/restart the OpenClaw gateway service with the root systemd user environment (`XDG_RUNTIME_DIR=/run/user/0`) so the bind change takes effect.

Verification steps:
1. Verify `openclaw gateway status --require-rpc` reports a running gateway and read probe OK.
2. Verify `ss -ltnp` shows OpenClaw listening on `0.0.0.0:18789` or equivalent wildcard listener.
3. Verify operator-side LAN HTTP reachability to `http://miloco.esxi:18789/` without printing page content or any credential. A 200, 401, 403, or other HTTP response proves reachability; connection refusal does not.
4. Verify the Miloco plugin still reports `Status: loaded` and `allowConversationAccess: true`.
5. Verify Miloco backend health at `http://miloco.esxi:1810/health` remains OK.
6. Close the CO with the actual outcome and update project progress plus workspace memory.
