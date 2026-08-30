"""Dashboard authentication recovery commands."""

import sys
from typing import Any

import click

from miloco_cli.client import api_get, api_post
from miloco_cli.commands._ordered_group import OrderedGroup
from miloco_cli.output import print_result


@click.group("auth", cls=OrderedGroup)
def auth_group() -> None:
    """Dashboard 用户鉴权管理。"""


def _read_password_from_stdin(password_stdin: bool) -> str:
    if not password_stdin:
        raise click.UsageError("provide --password-stdin and pipe the password on stdin")
    password = sys.stdin.readline().rstrip("\n")
    if not password:
        raise click.UsageError("password from stdin is empty")
    return password


def _setup_success_summary(result: dict) -> dict:
    """Keep the setup response useful without exposing its browser CSRF secret."""
    data = result.get("data")
    if not isinstance(data, dict):
        return result
    return {
        **result,
        "data": {key: value for key, value in data.items() if key != "csrf_token"},
    }


@auth_group.command("status")
@click.option("--pretty", is_flag=True)
def auth_status(pretty: bool) -> None:
    """Show whether dashboard administrator setup is required."""
    print_result(api_get("/api/auth/status"), pretty)


@auth_group.command("setup")
@click.option("--username", required=True)
@click.option("--display-name", default="")
@click.option("--password-stdin", is_flag=True)
@click.option("--pretty", is_flag=True)
def auth_setup(
    username: str, display_name: str, password_stdin: bool, pretty: bool
) -> None:
    """Create the first dashboard administrator."""
    password = _read_password_from_stdin(password_stdin)
    result = api_post(
        "/api/auth/setup",
        {
            "username": username,
            "display_name": display_name,
            "password": password,
            "password_confirm": password,
        },
        safe_errors=True,
        sensitive_values=(password,),
    )
    print_result(_setup_success_summary(result), pretty)


def _find_user_id(username: str) -> str:
    response = api_get("/api/users")
    data: Any = response.get("data", {}) if isinstance(response, dict) else {}
    users = data.get("users", []) if isinstance(data, dict) else []
    if not isinstance(users, list):
        raise click.ClickException("dashboard user list has an unexpected format")

    normalized = username.casefold()
    for user in users:
        if (
            isinstance(user, dict)
            and isinstance(user.get("username"), str)
            and user["username"].casefold() == normalized
            and user.get("id") is not None
        ):
            return str(user["id"])
    raise click.ClickException(f"dashboard user not found: {username}")


@auth_group.command("reset-password")
@click.option("--username", required=True)
@click.option("--password-stdin", is_flag=True)
@click.option("--pretty", is_flag=True)
def auth_reset_password(username: str, password_stdin: bool, pretty: bool) -> None:
    """Reset a dashboard user's password by username."""
    password = _read_password_from_stdin(password_stdin)
    user_id = _find_user_id(username)
    result = api_post(
        f"/api/users/{user_id}/password",
        {"password": password, "password_confirm": password},
        safe_errors=True,
        sensitive_values=(password,),
    )
    print_result(result, pretty)
