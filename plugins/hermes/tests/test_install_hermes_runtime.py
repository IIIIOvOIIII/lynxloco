"""Hermes shell 安装与验收脚本的 runtime 环境契约。"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

HERMES_DIR = Path(__file__).resolve().parent.parent
RUNTIME_HELPER = HERMES_DIR / "tests" / "_resolve_runtime_env.sh"
INSTALL_SCRIPT = HERMES_DIR / "install-hermes.sh"


def _run_runtime_helper(env: dict[str, str]) -> tuple[str, str]:
    result = subprocess.run(
        [
            "bash",
            "-c",
            '. "$1"; resolve_miloco_runtime_env; '
            'printf "%s\\n%s" "$MILOCO_HOME" "${MILOCO_TOKEN:-}"',
            "bash",
            str(RUNTIME_HELPER),
        ],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    home, token = result.stdout.split("\n", 1)
    return home, token


def test_shell_runtime_profile_overlays_default_pointer(tmp_path: Path):
    fake_user_home = tmp_path / "user"
    default = fake_user_home / ".config" / "miloco" / "default.env"
    default.parent.mkdir(parents=True)
    default.write_text(
        "MILOCO_HOME='/data/default miloco'\nMILOCO_TOKEN=default\n",
        encoding="utf-8",
    )
    profile = tmp_path / "profile.env"
    profile.write_text("MILOCO_TOKEN=profile\n", encoding="utf-8")
    env = os.environ.copy()
    env.pop("MILOCO_HOME", None)
    env.update(
        {
            "HOME": str(fake_user_home),
            "HERMES_HOME": str(tmp_path / "hermes"),
            "MILOCO_RUNTIME_ENV": str(profile),
        }
    )

    home, token = _run_runtime_helper(env)

    assert home == "/data/default miloco"
    assert token == "profile"


def test_shell_runtime_profile_can_override_default_home(tmp_path: Path):
    fake_user_home = tmp_path / "user"
    default = fake_user_home / ".config" / "miloco" / "default.env"
    default.parent.mkdir(parents=True)
    default.write_text("MILOCO_HOME=/data/default\n", encoding="utf-8")
    profile = tmp_path / "profile.env"
    profile.write_text("MILOCO_HOME='/data/profile home'\n", encoding="utf-8")
    env = os.environ.copy()
    env.pop("MILOCO_HOME", None)
    env.update(
        {
            "HOME": str(fake_user_home),
            "HERMES_HOME": str(tmp_path / "hermes"),
            "MILOCO_RUNTIME_ENV": str(profile),
        }
    )

    home, _ = _run_runtime_helper(env)

    assert home == "/data/profile home"


def _shell_rc_python() -> str:
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    section = text.split("# 写用户 shell rc", 1)[1].split(
        "# 改 supervisor conf", 1
    )[0]
    match = re.search(
        r'"\$PYTHON" - "\$_rc" "\$MILOCO_HOME" <<\'PY\'\n(.*?)\nPY',
        section,
        flags=re.DOTALL,
    )
    assert match, "找不到 shell rc 持久化的 Python heredoc"
    return match.group(1)


def test_shell_rc_round_trips_metacharacters(tmp_path: Path):
    rc = tmp_path / ".bashrc"
    rc.write_text("export MILOCO_HOME=/old\n", encoding="utf-8")
    value = str(tmp_path / 'cash$USER-`printf HACKED`-quote"-single\'-slash\\')

    subprocess.run(
        [sys.executable, "-", str(rc), value],
        input=_shell_rc_python(),
        text=True,
        check=True,
    )
    env = os.environ.copy()
    env.pop("MILOCO_HOME", None)
    loaded = subprocess.run(
        ["bash", "-c", '. "$1"; printf "%s" "$MILOCO_HOME"', "bash", str(rc)],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert loaded.stdout == value


def _supervisor_conf_python() -> str:
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    section = text.split("# 改 supervisor conf", 1)[1].split(
        "# supervisor reread", 1
    )[0]
    match = re.search(
        r'"\$PYTHON" - "\$SUPERVISORD_CONF" "\$MILOCO_HOME" <<\'PY\'\n(.*?)\nPY',
        section,
        flags=re.DOTALL,
    )
    assert match, "找不到 supervisord.conf 持久化的 Python heredoc"
    return match.group(1)


@pytest.mark.parametrize(
    "value",
    [
        "/data/line\nbreak",
        "/data/both'\"quotes",
        "/data/comment #truncated",
        "/data/comment ;truncated",
    ],
)
def test_supervisor_conf_rejects_unrepresentable_home(tmp_path: Path, value: str):
    conf = tmp_path / "supervisord.conf"
    original = 'environment=MILOCO_HOME="/old"\n'
    conf.write_text(original, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-", str(conf), value],
        input=_supervisor_conf_python(),
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert conf.read_text(encoding="utf-8") == original


def test_supervisor_conf_round_trips_supported_metacharacters(
    tmp_path: Path,
):
    conf = tmp_path / "supervisord.conf"
    conf.write_text('environment=MILOCO_HOME="/old"\n', encoding="utf-8")
    value = r'/data/100% "quoted"\path'

    subprocess.run(
        [sys.executable, "-", str(conf), value],
        input=_supervisor_conf_python(),
        text=True,
        check=True,
        capture_output=True,
    )

    assert f"MILOCO_HOME='{value.replace('%', '%%')}'" in conf.read_text(
        encoding="utf-8"
    )
