"""admin 命令组：status / home-info / cost"""

import json
import sys

import click

from miloco_cli.commands._ordered_group import OrderedGroup
from miloco_cli.output import print_result


@click.group("admin", cls=OrderedGroup)
def admin_group():
    """系统管理：状态 / 家庭信息 / 成本。"""


@admin_group.command("status")
@click.option("--pretty", is_flag=True)
def admin_status(pretty):
    """系统状态（MiOT 连接、SQLite、感知模型、规则引擎）。"""
    from miloco_cli.client import api_get

    data = api_get("/api/admin/status")
    print_result(data, pretty)


@admin_group.command("home-info")
@click.option("--pretty", is_flag=True)
def admin_home_info(pretty):
    """展示家庭信息摘要：设备 / 区域 / 场景 / 成员数量。"""
    from miloco_cli.home_info import get_home_info

    info = get_home_info()
    print_result({
        "devices": len(info.get("devices", [])),
        "areas": len(info.get("areas", [])),
        "scenes": len(info.get("scenes", [])),
        "persons": len(info.get("persons", [])),
    }, pretty)


@admin_group.command("cost")
@click.option(
    "--period",
    type=click.Choice(["today", "month"]),
    default="today",
    show_default=True,
    help="统计周期",
)
@click.option("--pretty", is_flag=True)
def admin_cost(period, pretty):
    """感知 LLM 调用成本统计。"""
    print(json.dumps({"code": 501, "message": "cost statistics not yet supported"}), file=sys.stderr)
    sys.exit(1)


_OMNI_PROTOCOL = click.Choice(
    ["openai_chat_completions", "openai_responses", "gemini_native"],
    case_sensitive=True,
)


@admin_group.group("omni", cls=OrderedGroup)
def admin_omni():
    """Manage saved Omni perception model profiles."""


def _omni_profile_options(command):
    command = click.option(
        "--api-key", default="", help="API Key; Responses may omit it."
    )(command)
    command = click.option("--api-protocol", type=_OMNI_PROTOCOL, required=True)(command)
    command = click.option("--base-url", required=True)(command)
    command = click.option("--model", required=True)(command)
    return click.option("--label", required=True)(command)


@admin_omni.command("create")
@_omni_profile_options
@click.option("--activate/--no-activate", default=True, show_default=True)
def omni_create(label, model, base_url, api_protocol, api_key, activate):
    """Create or replace a profile through the backend's shared config store."""
    from miloco_cli.client import api_put

    data = api_put(
        "/api/admin/omni-config",
        {
            "label": label,
            "model": model,
            "base_url": base_url,
            "api_protocol": api_protocol,
            "api_key": api_key,
            "activate": activate,
        },
    )
    print_result(data, pretty=False)


@admin_omni.command("test")
@_omni_profile_options
def omni_test(label, model, base_url, api_protocol, api_key):
    """Run the protocol-specific Omni visual preflight without saving."""
    from miloco_cli.client import api_post

    data = api_post(
        "/api/admin/omni-config/test",
        {
            "label": label,
            "model": model,
            "base_url": base_url,
            "api_protocol": api_protocol,
            "api_key": api_key,
        },
    )
    print_result(data, pretty=False)


@admin_omni.command("select")
@click.option("--label", required=True)
def omni_select(label):
    """Activate an existing profile by label."""
    from miloco_cli.client import api_post

    data = api_post("/api/admin/omni-config/activate", {"label": label})
    print_result(data, pretty=False)
