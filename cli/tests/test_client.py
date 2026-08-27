"""client.py 测试：HTTP 请求封装、错误处理。"""

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from miloco_cli.client import api_delete, api_get, api_patch, api_post, api_put

# ─── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """隔离配置，使用 localhost 默认地址。"""
    config_dir = tmp_path / "miloco"
    # 清空所有 MILOCO_* 环境变量避免污染测试
    import os as _os
    for key in list(_os.environ):
        if key.startswith("MILOCO_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MILOCO_HOME", str(config_dir))


def _make_response(data: dict, status_code: int = 200) -> MagicMock:
    """构造 httpx.Response mock。"""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.is_success = 200 <= status_code < 300
    resp.json.return_value = data
    resp.text = json.dumps(data)
    return resp


def _patch_client(resp: MagicMock):
    """patch httpx.Client，让所有 HTTP 方法返回指定 response。"""
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.get.return_value = resp
    mock_client.post.return_value = resp
    mock_client.put.return_value = resp
    mock_client.patch.return_value = resp
    mock_client.delete.return_value = resp
    mock_client.request.return_value = resp
    return patch("miloco_cli.client.httpx.Client", return_value=mock_client), mock_client


# ─── api_get ──────────────────────────────────────────────────────────────────


def test_api_get_success():
    resp = _make_response({"code": 0, "data": {"items": []}})
    patcher, mock_client = _patch_client(resp)
    with patcher:
        result = api_get("/api/test")
    mock_client.get.assert_called_once_with("/api/test", params=None)
    assert result["code"] == 0


def test_api_get_with_params():
    resp = _make_response({"code": 0})
    patcher, mock_client = _patch_client(resp)
    with patcher:
        api_get("/api/test", params={"key": "val"})
    mock_client.get.assert_called_once_with("/api/test", params={"key": "val"})


def test_api_get_connection_error_exits_2():
    with patch("miloco_cli.client.httpx.Client") as MockClient:
        MockClient.return_value.__enter__.side_effect = httpx.ConnectError("refused")
        with pytest.raises(SystemExit) as exc:
            api_get("/api/test")
        assert exc.value.code == 2


def test_api_get_timeout_error_exits_2():
    """C1 修复：超时等网络错误应退出码 2，而非抛出堆栈。"""
    with patch("miloco_cli.client.httpx.Client") as MockClient:
        MockClient.return_value.__enter__.side_effect = httpx.TimeoutException("timeout")
        with pytest.raises(SystemExit) as exc:
            api_get("/api/test")
        assert exc.value.code == 2


def test_api_get_read_error_exits_2():
    with patch("miloco_cli.client.httpx.Client") as MockClient:
        MockClient.return_value.__enter__.side_effect = httpx.ReadError("read error")
        with pytest.raises(SystemExit) as exc:
            api_get("/api/test")
        assert exc.value.code == 2


# ─── HTTP 4xx/5xx 错误处理 ────────────────────────────────────────────────────


def test_api_get_http_422_exits_3():
    """FastAPI 422 校验错误应退出码 3。"""
    resp = _make_response({"detail": [{"msg": "field required"}]}, status_code=422)
    patcher, _ = _patch_client(resp)
    with patcher:
        with pytest.raises(SystemExit) as exc:
            api_get("/api/test")
    assert exc.value.code == 3


def test_api_get_http_500_exits_3():
    resp = _make_response({"detail": "Internal Server Error"}, status_code=500)
    patcher, _ = _patch_client(resp)
    with patcher:
        with pytest.raises(SystemExit) as exc:
            api_get("/api/test")
    assert exc.value.code == 3


def test_api_get_business_error_exits_3():
    """业务 code != 0 应退出码 3。"""
    resp = _make_response({"code": 404, "message": "not found"})
    patcher, _ = _patch_client(resp)
    with patcher:
        with pytest.raises(SystemExit) as exc:
            api_get("/api/test")
    assert exc.value.code == 3


def test_safe_error_mode_emits_only_stable_code_and_message(capsys):
    resp = _make_response(
        {
            "detail": {
                "code": "authentication_failed",
                "message": "RTSP authentication failed",
            }
        },
        status_code=409,
    )
    patcher, _ = _patch_client(resp)
    with patcher, pytest.raises(SystemExit) as exc:
        api_post(
            "/api/cameras/rtsp",
            {"password": "synthetic-camera-secret"},
            safe_errors=True,
            sensitive_values=("synthetic-camera-secret",),
        )

    assert exc.value.code == 3
    assert json.loads(capsys.readouterr().err) == {
        "error": {
            "code": "authentication_failed",
            "message": "RTSP authentication failed",
        }
    }


def test_safe_error_mode_checks_raw_quoted_and_escaped_credentials(capsys):
    password = 'quote"secret\\tail'
    username = 'user"name\\tail'
    resp = _make_response(
        {
            "detail": {
                "code": "authentication_failed",
                "message": f"echoed {password} for {username}",
            }
        },
        status_code=409,
    )
    patcher, _ = _patch_client(resp)
    with patcher, pytest.raises(SystemExit) as exc:
        api_post(
            "/api/cameras/rtsp",
            {"password": password, "username": username},
            safe_errors=True,
            sensitive_values=(password, username),
        )

    assert exc.value.code == 3
    captured = capsys.readouterr()
    payload = json.loads(captured.err)
    assert payload == {
        "error": {
            "code": "camera_request_failed",
            "message": "Camera request failed",
        }
    }
    assert password not in repr(payload)
    assert username not in repr(payload)


def test_safe_error_mode_replaces_unsafe_schema_with_generic_error(capsys):
    secret = "synthetic-camera-secret"
    username = "camera-user"
    userinfo_uri = f"rtsp://{username}:{secret}@camera.local/live"
    resp = _make_response(
        {
            "detail": {
                "code": "proxy_error",
                "message": f"echo {secret} {username} {userinfo_uri}",
                "request": {"password": secret},
            }
        },
        status_code=502,
    )
    patcher, _ = _patch_client(resp)
    with patcher, pytest.raises(SystemExit) as exc:
        api_post(
            "/api/cameras/rtsp",
            {"password": secret},
            safe_errors=True,
            sensitive_values=(secret, username, "rtsp://camera.local/live"),
        )

    assert exc.value.code == 3
    captured = capsys.readouterr()
    assert json.loads(captured.err) == {
        "error": {
            "code": "camera_request_failed",
            "message": "Camera request failed",
        }
    }
    assert secret not in captured.err
    assert username not in captured.err
    assert userinfo_uri not in captured.err


def test_safe_error_mode_invalid_json_never_prints_response_text(capsys):
    secret = "synthetic-camera-secret"
    resp = _make_response({}, status_code=502)
    resp.json.side_effect = ValueError(f"invalid response containing {secret}")
    resp.text = f"proxy response containing {secret}"
    patcher, _ = _patch_client(resp)
    with patcher, pytest.raises(SystemExit) as exc:
        api_get(
            "/api/cameras",
            safe_errors=True,
            sensitive_values=(secret,),
        )

    assert exc.value.code == 3
    captured = capsys.readouterr()
    assert json.loads(captured.err) == {
        "error": {
            "code": "camera_request_failed",
            "message": "Camera request failed",
        }
    }
    assert secret not in captured.err


def test_safe_error_mode_nonzero_success_envelope_is_generic(capsys):
    secret = "synthetic-camera-secret"
    resp = _make_response({"code": 9, "message": f"echo {secret}"})
    patcher, _ = _patch_client(resp)
    with patcher, pytest.raises(SystemExit) as exc:
        api_get(
            "/api/cameras",
            safe_errors=True,
            sensitive_values=(secret,),
        )

    assert exc.value.code == 3
    captured = capsys.readouterr()
    assert json.loads(captured.err) == {
        "error": {
            "code": "camera_request_failed",
            "message": "Camera request failed",
        }
    }
    assert secret not in captured.err


# ─── api_post / api_put / api_patch ──────────────────────────────────────────


def test_api_post_sends_body():
    resp = _make_response({"code": 0})
    patcher, mock_client = _patch_client(resp)
    with patcher:
        api_post("/api/resource", {"name": "test"})
    mock_client.post.assert_called_once_with("/api/resource", json={"name": "test"})


def test_api_post_empty_body_sends_empty_dict():
    resp = _make_response({"code": 0})
    patcher, mock_client = _patch_client(resp)
    with patcher:
        api_post("/api/resource", None)
    mock_client.post.assert_called_once_with("/api/resource", json={})


def test_api_put_sends_body():
    resp = _make_response({"code": 0})
    patcher, mock_client = _patch_client(resp)
    with patcher:
        api_put("/api/resource/1", {"name": "updated"})
    mock_client.put.assert_called_once_with("/api/resource/1", json={"name": "updated"})


def test_api_patch_sends_body():
    resp = _make_response({"code": 0})
    patcher, mock_client = _patch_client(resp)
    with patcher:
        api_patch("/api/resource/1", {"enabled": True})
    mock_client.patch.assert_called_once_with("/api/resource/1", json={"enabled": True})


# ─── api_delete ───────────────────────────────────────────────────────────────


def test_api_delete_success():
    resp = _make_response({"code": 0})
    patcher, mock_client = _patch_client(resp)
    with patcher:
        result = api_delete("/api/resource/1")
    mock_client.delete.assert_called_once_with("/api/resource/1", params=None)
    assert result["code"] == 0


def test_api_delete_with_params():
    """M12 修复：api_delete 支持 params 参数（prompt-clear 等批量删除走 ?did=a&did=b）。"""
    resp = _make_response({"code": 0})
    patcher, mock_client = _patch_client(resp)
    with patcher:
        api_delete("/api/rules/logs", params={"keep_days": 7})
    mock_client.delete.assert_called_once_with(
        "/api/rules/logs", params={"keep_days": 7}
    )


# ─── tls_verify ───────────────────────────────────────────────────────────────


def test_tls_verify_false_by_default():
    """tls_verify 默认为 false，httpx.Client verify 应为 False。"""
    resp = _make_response({"code": 0})
    patcher, _ = _patch_client(resp)
    with patcher as MockClient:
        api_get("/api/test")
    assert MockClient.call_args[1]["verify"] is False


def test_tls_verify_true_when_configured():
    """tls_verify=true 时，httpx.Client verify 应为 True。"""
    from miloco_cli.config import set_value
    set_value("server.tls_verify", "true")
    resp = _make_response({"code": 0})
    patcher, _ = _patch_client(resp)
    with patcher as MockClient:
        api_get("/api/test")
    assert MockClient.call_args[1]["verify"] is True
