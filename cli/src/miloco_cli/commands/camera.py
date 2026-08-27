"""Credential-safe camera management commands."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from typing import Any, NoReturn

import click

from miloco_cli.output import print_result

_API_PREFIX = "/api/cameras"
_REDACTED = "[REDACTED]"
_SENSITIVE_KEYS = frozenset({"password", "username", "uri"})
_RTSP_URI = re.compile(r"rtsps?://[^\s\"']+", re.IGNORECASE)


def _exit_with_error(message: str, code: int) -> NoReturn:
    click.echo(json.dumps({"error": message}, ensure_ascii=False), err=True)
    raise click.exceptions.Exit(code)


def _require(value: str | None, option: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _exit_with_error(f"{option} is required", 1)
    return value


def _read_password(password_stdin: bool) -> str:
    if not password_stdin:
        return ""
    try:
        line = sys.stdin.readline()
    except (EOFError, OSError):
        line = ""
    password = line.rstrip("\r\n")
    if not password:
        _exit_with_error("--password-stdin requires one non-empty input line", 1)
    return password


def _redact_output(value: Any, sensitive_values: tuple[str, ...] = ()) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                _REDACTED
                if str(key).lower() in _SENSITIVE_KEYS
                else _redact_output(item, sensitive_values)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_output(item, sensitive_values) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_output(item, sensitive_values) for item in value)
    if isinstance(value, str):
        redacted = _RTSP_URI.sub(_REDACTED, value)
        for sensitive in sensitive_values:
            if sensitive:
                redacted = redacted.replace(sensitive, _REDACTED)
        return redacted
    return value


def _request_and_print(
    request: Callable[[], Any],
    *,
    pretty: bool,
    sensitive_values: tuple[str, ...] = (),
) -> None:
    try:
        data = request()
    except (SystemExit, click.exceptions.Exit):
        raise
    except Exception:  # noqa: BLE001 - discard input-bearing dependency errors
        _exit_with_error("camera_request_failed: Camera request failed", 3)
    print_result(_redact_output(data, sensitive_values), pretty)


def _upsert_body(
    *,
    name: str,
    room: str,
    uri: str,
    username: str,
    password: str,
    transport: str,
    audio_enabled: bool,
) -> dict[str, Any]:
    return {
        "name": name,
        "room_name": room,
        "uri": uri,
        "username": username,
        "password": password,
        "transport": transport,
        "audio_enabled": audio_enabled,
    }


def _management_options(command):
    command = click.option("--pretty", is_flag=True)(command)
    command = click.option(
        "--audio/--no-audio", "audio_enabled", default=True, show_default=True
    )(command)
    command = click.option(
        "--transport",
        type=click.Choice(["tcp", "udp"]),
        default="tcp",
        show_default=True,
    )(command)
    command = click.option("--password-stdin", is_flag=True)(command)
    command = click.option("--username", default="")(command)
    command = click.option("--uri")(command)
    command = click.option("--room")(command)
    command = click.option("--name")(command)
    return command


@click.group("camera")
def camera_group() -> None:
    """List and manage camera sources."""


@camera_group.command("list")
@click.option("--pretty", is_flag=True)
def camera_list(pretty: bool) -> None:
    """List cameras from all configured sources."""
    from miloco_cli.client import api_get

    _request_and_print(lambda: api_get(_API_PREFIX), pretty=pretty)


@click.group("rtsp")
def rtsp_group() -> None:
    """Test, add, and edit RTSP camera sources."""


@rtsp_group.command("test")
@click.option("--uri")
@click.option("--username", default="")
@click.option("--password-stdin", is_flag=True)
@click.option(
    "--transport",
    type=click.Choice(["tcp", "udp"]),
    default="tcp",
    show_default=True,
)
@click.option("--audio/--no-audio", "audio_enabled", default=True, show_default=True)
@click.option("--pretty", is_flag=True)
def rtsp_test(
    uri: str | None,
    username: str,
    password_stdin: bool,
    transport: str,
    audio_enabled: bool,
    pretty: bool,
) -> None:
    """Probe an RTSP source without saving it."""
    from miloco_cli.client import api_post

    validated_uri = _require(uri, "--uri")
    password = _read_password(password_stdin)
    body = _upsert_body(
        name="",
        room="",
        uri=validated_uri,
        username=username,
        password=password,
        transport=transport,
        audio_enabled=audio_enabled,
    )
    _request_and_print(
        lambda: api_post(f"{_API_PREFIX}/rtsp/test", body),
        pretty=pretty,
        sensitive_values=(password, username, validated_uri),
    )


@rtsp_group.command("add")
@_management_options
def rtsp_add(
    name: str | None,
    room: str | None,
    uri: str | None,
    username: str,
    password_stdin: bool,
    transport: str,
    audio_enabled: bool,
    pretty: bool,
) -> None:
    """Save a new disabled RTSP source."""
    from miloco_cli.client import api_post

    validated_name = _require(name, "--name")
    validated_room = _require(room, "--room")
    validated_uri = _require(uri, "--uri")
    password = _read_password(password_stdin)
    body = _upsert_body(
        name=validated_name,
        room=validated_room,
        uri=validated_uri,
        username=username,
        password=password,
        transport=transport,
        audio_enabled=audio_enabled,
    )
    _request_and_print(
        lambda: api_post(f"{_API_PREFIX}/rtsp", body),
        pretty=pretty,
        sensitive_values=(password, username, validated_uri),
    )


@rtsp_group.command("edit")
@click.argument("camera_id", required=False)
@_management_options
def rtsp_edit(
    camera_id: str | None,
    name: str | None,
    room: str | None,
    uri: str | None,
    username: str,
    password_stdin: bool,
    transport: str,
    audio_enabled: bool,
    pretty: bool,
) -> None:
    """Replace an RTSP source definition while preserving a blank password."""
    from miloco_cli.client import api_put

    validated_id = _require(camera_id, "camera id")
    validated_name = _require(name, "--name")
    validated_room = _require(room, "--room")
    validated_uri = _require(uri, "--uri")
    password = _read_password(password_stdin)
    body = _upsert_body(
        name=validated_name,
        room=validated_room,
        uri=validated_uri,
        username=username,
        password=password,
        transport=transport,
        audio_enabled=audio_enabled,
    )
    _request_and_print(
        lambda: api_put(f"{_API_PREFIX}/rtsp/{validated_id}", body),
        pretty=pretty,
        sensitive_values=(password, username, validated_uri),
    )


camera_group.add_command(rtsp_group)


def _state_command(action: str, camera_id: str | None, pretty: bool) -> None:
    from miloco_cli.client import api_post

    validated_id = _require(camera_id, "camera id")
    _request_and_print(
        lambda: api_post(f"{_API_PREFIX}/{validated_id}/{action}"), pretty=pretty
    )


@camera_group.command("enable")
@click.argument("camera_id", required=False)
@click.option("--pretty", is_flag=True)
def camera_enable(camera_id: str | None, pretty: bool) -> None:
    """Probe and enable a camera source."""
    _state_command("enable", camera_id, pretty)


@camera_group.command("disable")
@click.argument("camera_id", required=False)
@click.option("--pretty", is_flag=True)
def camera_disable(camera_id: str | None, pretty: bool) -> None:
    """Disable a camera source."""
    _state_command("disable", camera_id, pretty)


@camera_group.command("delete")
@click.argument("camera_id", required=False)
@click.option("--yes", is_flag=True, help="Confirm permanent deletion.")
@click.option("--pretty", is_flag=True)
def camera_delete(camera_id: str | None, yes: bool, pretty: bool) -> None:
    """Delete a camera source after explicit confirmation."""
    from miloco_cli.client import api_delete

    validated_id = _require(camera_id, "camera id")
    if not yes:
        _exit_with_error("--yes is required to delete a camera", 1)
    _request_and_print(
        lambda: api_delete(f"{_API_PREFIX}/{validated_id}"), pretty=pretty
    )
