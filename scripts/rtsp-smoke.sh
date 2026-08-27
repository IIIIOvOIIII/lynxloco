#!/usr/bin/env bash
# Local/lab-only RTSP acceptance smoke. Credentials are read only from env.
set +x
set -euo pipefail

RTSP_URL="${MILOCO_RTSP_TEST_URL:-}"
RTSP_USERNAME="${MILOCO_RTSP_TEST_USERNAME:-}"
RTSP_PASSWORD="${MILOCO_RTSP_TEST_PASSWORD:-}"
CAMERA_ID=""
SMOKE_TIMEOUT_SEC="${MILOCO_RTSP_TEST_TIMEOUT_SEC:-30}"

if [[ -z "$RTSP_URL" ]]; then
    echo "MILOCO_RTSP_TEST_URL is required; refusing to mutate camera configuration" >&2
    exit 2
fi
if [[ ! "$RTSP_URL" =~ ^rtsps?:// ]]; then
    echo "MILOCO_RTSP_TEST_URL must use rtsp:// or rtsps://" >&2
    exit 2
fi
if [[ ! "$SMOKE_TIMEOUT_SEC" =~ ^[1-9][0-9]*$ ]]; then
    echo "MILOCO_RTSP_TEST_TIMEOUT_SEC must be a positive integer" >&2
    exit 2
fi

for command_name in miloco-cli python3; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "$command_name is required" >&2
        exit 2
    fi
done

cleanup() {
    local cleanup_id="$CAMERA_ID"
    CAMERA_ID=""
    if [[ "$cleanup_id" =~ ^rtsp:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]]; then
        miloco-cli camera delete "$cleanup_id" --yes >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT INT TERM HUP

run_with_optional_password() {
    if [[ -n "$RTSP_PASSWORD" ]]; then
        printf '%s\n' "$RTSP_PASSWORD" | "$@" --password-stdin
    else
        "$@"
    fi
}

json_field() {
    local expression="$1"
    python3 -c '
import json
import sys

payload = json.load(sys.stdin)
value = payload
for key in sys.argv[1].split("."):
    value = value[key]
print(value)
' "$expression"
}

TEMP_NAME="miloco-rtsp-smoke-$$"
add_output="$({
    run_with_optional_password \
        miloco-cli camera rtsp add \
        --name "$TEMP_NAME" \
        --room "RTSP smoke" \
        --uri "$RTSP_URL" \
        --username "$RTSP_USERNAME" \
        --transport tcp \
        --audio
} 2>/dev/null)"
CAMERA_ID="$(printf '%s' "$add_output" | json_field data.id)"
if [[ ! "$CAMERA_ID" =~ ^rtsp:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]]; then
    CAMERA_ID=""
    echo "Backend did not return a temporary RTSP UUID; refusing further mutation" >&2
    exit 3
fi

test_output="$({
    run_with_optional_password \
        miloco-cli camera rtsp test \
        --uri "$RTSP_URL" \
        --username "$RTSP_USERNAME" \
        --transport tcp \
        --audio
} 2>/dev/null)"
probe_summary="$(printf '%s' "$test_output" | python3 -c '
import json
import sys

data = json.load(sys.stdin)["data"]
print(f"{data[\"video_codec\"]}\t{int(data[\"width\"])}\t{int(data[\"height\"])}")
')"
IFS=$'\t' read -r VIDEO_CODEC WIDTH HEIGHT <<<"$probe_summary"

START_NS="$(python3 -c 'import time; print(time.monotonic_ns())')"
miloco-cli camera enable "$CAMERA_ID" >/dev/null

deadline=$((SECONDS + SMOKE_TIMEOUT_SEC))
while (( SECONDS < deadline )); do
    list_output="$(miloco-cli camera list 2>/dev/null)"
    if printf '%s' "$list_output" | python3 -c '
import json
import sys

rows = json.load(sys.stdin).get("data", [])
camera_id = sys.argv[1]
raise SystemExit(0 if any(
    row.get("id") == camera_id
    and row.get("enabled") is True
    and row.get("connected") is True
    and bool(row.get("video_codec"))
    for row in rows
) else 1)
' "$CAMERA_ID"; then
        END_NS="$(python3 -c 'import time; print(time.monotonic_ns())')"
        STARTUP_MS=$(((END_NS - START_NS) / 1000000))
        printf 'codec=%s dimensions=%sx%s startup_ms=%s reconnect=not_measured\n' \
            "$VIDEO_CODEC" "$WIDTH" "$HEIGHT" "$STARTUP_MS"
        exit 0
    fi
    sleep 1
done

echo "Temporary RTSP source did not reach decoded-connected state before timeout" >&2
exit 4
