#!/usr/bin/env bash
# Hermes 验收/e2e 脚本共用的 MILOCO_HOME 解析契约。
# 调用方必须先设置 HERMES_HOME，再调用 resolve_miloco_runtime_env。

resolve_miloco_runtime_env() {
  # 显式环境变量最高优先级；与 miloco_cli.config.bootstrap_runtime_env 一致，
  # 此时不再加载 pointer/profile 中的其它键。
  if [ -n "${MILOCO_HOME:-}" ]; then
    export MILOCO_HOME
    return
  fi

  local default_runtime_env="$HOME/.config/miloco/default.env"
  local profile_runtime_env="${MILOCO_RUNTIME_ENV:-}"
  if [ "${profile_runtime_env:0:2}" = "~/" ]; then
    profile_runtime_env="$HOME/${profile_runtime_env:2}"
  fi

  # 默认指针先加载，profile 后加载，因此 profile 中出现的键覆盖默认值，
  # profile 缺失的键则继续沿用默认指针。
  if [ -f "$default_runtime_env" ]; then
    # shellcheck disable=SC1090
    . "$default_runtime_env"
  fi
  if [ -n "$profile_runtime_env" ] \
    && [ "$profile_runtime_env" != "$default_runtime_env" ] \
    && [ -f "$profile_runtime_env" ]; then
    # shellcheck disable=SC1090
    . "$profile_runtime_env"
  fi

  MILOCO_HOME="${MILOCO_HOME:-${HERMES_HOME}/miloco}"
  export MILOCO_HOME
}
