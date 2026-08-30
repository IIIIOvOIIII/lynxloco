"""miloco-devices skill safety wording tests."""

from __future__ import annotations

from pathlib import Path


def test_miloco_device_skill_mentions_home_assistant_safety() -> None:
    text = Path("plugins/skills/miloco-devices/SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "Home Assistant" in text
    assert "arbitrary HA service" in text
    assert "Do not invent Home Assistant entity IDs" in text
