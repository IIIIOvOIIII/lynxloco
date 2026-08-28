# Predeployment Security Fix Report

Date: 2026-08-28
Worktree: `/Users/nicholasliao/clawd/xiaomi-miloco/.worktrees/rtsp-responses-support`
Scope: `miloco-cli config show` nested credential redaction
Planned commit: `fix(cli): redact nested camera and model credentials`

## RED

- Added `test_config_show_masks_nested_camera_and_profile_credentials` in `cli/tests/test_commands.py`.
- Added `test_config_show_unmasked_preserves_nested_camera_and_profile_credentials` in `cli/tests/test_commands.py`.
- First valid RED command:

```bash
PYTHONPATH=src ../backend/.venv/bin/pytest -q tests/test_commands.py -k "nested_camera_and_profile_credentials"
```

- RED result:
  - `1 failed, 1 passed, 164 deselected`
  - Failure proved default `config show` echoed `camera.rtsp_sources[0].uri`, `username`, `password` in raw output.

## GREEN

- Minimal production change in `cli/src/miloco_cli/commands/config.py`:
  - keep existing `server.token` and `model.omni.api_key` masking
  - additionally redact `camera.rtsp_sources[*].uri`
  - additionally redact `camera.rtsp_sources[*].username`
  - additionally redact `camera.rtsp_sources[*].password`
  - additionally redact `model.omni_profiles[*].api_key`
  - preserve missing fields and `--unmasked` bypass semantics

- GREEN command:

```bash
PYTHONPATH=src ../backend/.venv/bin/pytest -q tests/test_commands.py -k "nested_camera_and_profile_credentials"
```

- GREEN result:
  - `2 passed, 164 deselected`

## Verification

- Focused behavior:

```bash
PYTHONPATH=src ../backend/.venv/bin/pytest -q tests/test_commands.py -k "nested_camera_and_profile_credentials"
```

  - Result: `2 passed, 164 deselected`

- Full CLI pytest:

```bash
PYTHONPATH=src ../backend/.venv/bin/pytest -q tests
```

  - Result: `646 passed in 3.22s`

- CLI Ruff:

```bash
../backend/.venv/bin/ruff check src tests
```

  - Result: `All checks passed!`

- Diff integrity:

```bash
git diff --check
```

  - Result: clean

- Leak scan on modified diff:

```bash
git diff -- cli/src/miloco_cli/commands/config.py cli/tests/test_commands.py | rg -n "rtsp://[^[:space:]\"']+:[^[:space:]\"']+@|sk-[A-Za-z0-9]{10,}|BEGIN (RSA|OPENSSH|EC) PRIVATE KEY|AKIA[0-9A-Z]{16}"
```

  - Result: no matches

- Sentinel-only scan:

```bash
rg -n "redaction-camera|redaction-profile|unmasked-camera|unmasked-profile|secret-token" cli/src cli/tests
```

  - Result: matches only the intended test sentinel strings in `cli/tests/test_commands.py`

## Files

- `cli/src/miloco_cli/commands/config.py`
- `cli/tests/test_commands.py`
- `.superpowers/sdd/2026-08-28-ai-lab-deployment/predeployment-security-fix-report.md`

## Concerns

- None for this fix scope.
- This change only covers CLI display redaction for `config show`; it does not alter persisted config content or backend runtime handling.
