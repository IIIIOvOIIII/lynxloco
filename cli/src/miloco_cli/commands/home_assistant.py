"""Home Assistant 接入命令组。"""

from __future__ import annotations

from urllib.parse import quote

import click

from miloco_cli.output import print_result


@click.group("home-assistant")
def home_assistant_group():
    """Home Assistant 接入：状态、测试、同步、导入与控制权限。"""


@home_assistant_group.command("status")
@click.option("--pretty", is_flag=True)
def status(pretty):
    """查看 Home Assistant 接入状态。"""
    from miloco_cli.client import api_get

    print_result(api_get("/api/home-assistant/status"), pretty)


@home_assistant_group.command("test")
@click.option("--url", "base_url", required=True, help="Home Assistant Base URL")
@click.option(
    "--token-stdin",
    is_flag=True,
    help="从 stdin 读取长期访问令牌，避免进入 shell history",
)
@click.option(
    "--no-verify-tls",
    "verify_tls",
    is_flag=True,
    default=False,
    help="跳过 HTTPS 证书校验（仅自签名内网测试时使用）",
)
@click.option("--pretty", is_flag=True)
def test(base_url, token_stdin, verify_tls, pretty):
    """测试 Home Assistant 连接但不保存配置。"""
    from miloco_cli.client import api_post

    token = _read_token(token_stdin)
    result = api_post(
        "/api/home-assistant/test",
        {
            "enabled": True,
            "base_url": base_url,
            "token": token,
            "verify_tls": not verify_tls,
        },
    )
    print_result(result, pretty)


@home_assistant_group.command("connect")
@click.option("--url", "base_url", required=True, help="Home Assistant Base URL")
@click.option(
    "--token-stdin",
    is_flag=True,
    help="从 stdin 读取长期访问令牌，避免进入 shell history",
)
@click.option(
    "--disabled",
    is_flag=True,
    default=False,
    help="保存配置但暂不启用 Home Assistant 接入",
)
@click.option(
    "--no-verify-tls",
    "verify_tls",
    is_flag=True,
    default=False,
    help="跳过 HTTPS 证书校验（仅自签名内网测试时使用）",
)
@click.option("--pretty", is_flag=True)
def connect(base_url, token_stdin, disabled, verify_tls, pretty):
    """保存 Home Assistant 连接配置。"""
    from miloco_cli.client import api_post

    token = _read_token(token_stdin)
    result = api_post(
        "/api/home-assistant/config",
        {
            "enabled": not disabled,
            "base_url": base_url,
            "token": token,
            "verify_tls": not verify_tls,
        },
    )
    print_result(result, pretty)


@home_assistant_group.command("refresh")
@click.option("--pretty", is_flag=True)
def refresh(pretty):
    """刷新并列出 Home Assistant 实体发现结果。"""
    from miloco_cli.client import api_get

    print_result(
        api_get("/api/home-assistant/entities", {"refresh": "true"}),
        pretty,
    )


@home_assistant_group.command("import")
@click.argument("entity_id")
@click.option("--pretty", is_flag=True)
def import_entity(entity_id, pretty):
    """将一个 HA entity 导入 Miloco（默认只读）。"""
    print_result(_put_policy(entity_id, included=True, control_enabled=False), pretty)


@home_assistant_group.command("remove")
@click.argument("entity_id")
@click.option("--pretty", is_flag=True)
def remove_entity(entity_id, pretty):
    """从 Miloco 隐藏一个 HA entity，不删除 HA 侧实体。"""
    print_result(_put_policy(entity_id, included=False, control_enabled=False), pretty)


@home_assistant_group.command("enable-control")
@click.argument("entity_id")
@click.option("--pretty", is_flag=True)
def enable_control(entity_id, pretty):
    """允许 Miloco 控制一个已导入的 HA entity。"""
    print_result(_put_policy(entity_id, included=True, control_enabled=True), pretty)


@home_assistant_group.command("disable-control")
@click.argument("entity_id")
@click.option("--pretty", is_flag=True)
def disable_control(entity_id, pretty):
    """保留导入但禁止 Miloco 控制一个 HA entity。"""
    print_result(_put_policy(entity_id, included=True, control_enabled=False), pretty)


def _put_policy(entity_id: str, *, included: bool, control_enabled: bool):
    from miloco_cli.client import api_put

    return api_put(
        f"/api/home-assistant/entities/{quote(entity_id, safe='')}/policy",
        {"included": included, "control_enabled": control_enabled},
    )


def _read_token(token_stdin: bool) -> str:
    if not token_stdin:
        raise click.UsageError("provide --token-stdin and pipe the HA token on stdin")
    return click.get_text_stream("stdin").readline().strip()

