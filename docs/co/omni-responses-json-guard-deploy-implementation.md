Deploy the exact Miloco source commit `f83d500c3acdfcc3465d6b2148b112e656a206e8` to `miloco.esxi` to repair realtime OpenAI Responses image-sequence inference returning malformed or semantically empty structured output.

Scope:
- Target host: `miloco.esxi`
- Target user: `root`
- Impacted service: Miloco official installation and OpenClaw Miloco plugin
- Source commit: `f83d500c3acdfcc3465d6b2148b112e656a206e8`
- Version: `2026.8.6.post1.dev224+gf83d500c3`
- This change does not modify camera configuration, Xiaomi account configuration, model endpoint configuration, model API keys, RTSP URLs, stored identity data, or the Miloco database schema.

Approved artifacts and SHA-256:
- `dist/install.sh`: `4e3878231376a3bdd5b38d794d69e03caf2129a93b3c9829a04059268cfac665`
- `dist/miloco-2026.8.6.post1.dev224+gf83d500c3-py3-none-any.whl`: `98d87ee5272ca27fe778d55164afdd33a59ea9647ff0a51d3fe0a36c5829dc97`
- `dist/miloco_cli-2026.8.6.post1.dev224+gf83d500c3-py3-none-any.whl`: `dce586ef8f335f1db1adf5a3d15e21e068bfd4efcbd0c8e06f7b7c51c5f89202`
- `dist/miloco_miot-2026.8.6.post1.dev224+gf83d500c3-py3-none-manylinux_2_28_x86_64.whl`: `7c147abc23bf4651da4601c0deac0c108067a931c124ffe96fefed2c54df9f32`
- `dist/miloco-openclaw-plugin-2026.8.6-post1.dev224.tgz`: `50af1e0c2a0600ce16d5e8dcdd93301c948fd8a95b35499a328898aeb0f674cc`

Implementation plan:
1. Verify this CO is in `Implement`, PAM is active for `miloco.esxi/root`, and the current local source commit matches `f83d500c3acdfcc3465d6b2148b112e656a206e8`.
2. Recompute local SHA-256 for the approved artifacts above.
3. Create a bounded remote staging directory named for this CO and source commit.
4. Upload only `dist/`, `scripts/`, and non-secret `plugins/skills/` content to that staging directory using tar-over-SSH. Do not copy workspace secrets, local credentials, Git metadata, or runtime databases.
5. Recompute remote SHA-256 for the approved artifacts and compare them with the CO values before installation.
6. Install `miloco`, `miloco-miot`, `miloco-cli`, and `miloco-openclaw-plugin` from the staged local artifacts using the official installer-compatible `uv tool install` / `openclaw plugins install` path.
7. Run Miloco skill-tool registration, refresh the OpenClaw plugin registry, restart the OpenClaw gateway, and restart the Miloco backend through the installed CLI/supervisor path.
8. Verify installed Miloco, Miloco MIoT, Miloco CLI, and OpenClaw plugin versions match `2026.8.6.post1.dev224+gf83d500c3` / `2026.8.6-post1.dev224`.
9. Verify local and LAN Miloco health, dashboard HTTP 200, OpenClaw gateway health, and `0.0.0.0` gateway bind.
10. Verify production realtime RTSP/OpenAI Responses inference using sanitized evidence only: request shape counts, HTTP status, JSON parse status, parser semantic counts, runtime-summary classification, and `perception_log.descriptions` non-empty state. Do not print RTSP URLs, API keys, raw prompts, raw model responses, or camera frame contents.

Success criteria:
- Source and artifact hash checks match this CO.
- Miloco and OpenClaw restart successfully.
- `/health` returns OK locally and over LAN.
- `/api/perception/runtime-summary` remains available.
- A live RTSP OpenAI Responses fused request returns valid JSON with at least one parsed caption.
- Realtime production diagnostics and/or fresh `perception_log.descriptions` show non-empty structured output after the restart window.
