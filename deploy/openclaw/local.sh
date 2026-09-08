#!/usr/bin/env bash
# Sourced only by deploy.sh after explicit MILOCO_DEPLOY_RUNTIME=openclaw.

readonly OPENCLAW_PROFILE="openclaw-root-v1"
readonly OPENCLAW_ROOT="/opt/miloco-openclaw"
native_controller_digest=""
native_payload_digest=""

openclaw_build_payload() {
    local sha="$1" plugin archive payload
    plugin="$(select_one "OpenClaw plugin" "$PROJECT_ROOT/dist/miloco-openclaw-plugin-"*.tgz)"
    archive="$PROJECT_ROOT/dist/lab/$sha/miloco-lab-${sha}.tar.gz"
    payload="$PROJECT_ROOT/dist/lab/$sha/miloco-native-${sha}.tar.gz"
    python3 - "$PROJECT_ROOT/deploy/openclaw/remote-release.py" "$archive" "$plugin" "$sha" "$payload" <<'PY'
from pathlib import Path
import runpy
import sys
runpy.run_path(sys.argv[1])["prepare_payload"](Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4], Path(sys.argv[5]))
PY
}

openclaw_assert_clean() {
    assert_clean_controller
    git ls-files --error-unmatch -- deploy/openclaw/local.sh deploy/openclaw/remote-release.py >/dev/null 2>&1 \
        || die 3 "native deployment controllers must be tracked"
    git diff --quiet "$clean_sha" -- deploy/openclaw/local.sh deploy/openclaw/remote-release.py \
        || die 3 "native deployment controllers must match clean Git HEAD"
}

openclaw_write_receipt() {
    local sha="$1" destination temporary
    read_release_receipt "$sha"
    destination="$PROJECT_ROOT/dist/lab/$sha/miloco-lab-${sha}.openclaw.receipt"
    [[ ! -e "$destination" && ! -L "$destination" ]] || die 4 "immutable native receipt already exists"
    temporary="$(mktemp "${destination}.XXXXXX")"
    local payload="$PROJECT_ROOT/dist/lab/$sha/miloco-native-${sha}.tar.gz"
    [[ -f "$payload" && ! -L "$payload" ]] || die 4 "native asset payload is missing"
    printf 'schema=2\ngit_sha=%s\nruntime_profile=%s\narchive_sha256=%s\ncontroller_sha256=%s\nallowlist_sha256=%s\npayload_sha256=%s\n' \
        "$sha" "$OPENCLAW_PROFILE" "$receipt_archive_digest" \
        "$(sha256_file "$PROJECT_ROOT/deploy/openclaw/remote-release.py")" "$receipt_allowlist_digest" \
        "$(sha256_file "$payload")" \
        > "$temporary"
    chmod 0444 "$temporary"
    mv -- "$temporary" "$destination"
}

openclaw_read_receipt() {
    local sha="$1" receipt line key value count=0 schema="" receipt_sha="" profile="" archive="" allowlist=""
    read_release_receipt "$sha"
    receipt="$PROJECT_ROOT/dist/lab/$sha/miloco-lab-${sha}.openclaw.receipt"
    [[ -f "$receipt" && ! -L "$receipt" ]] || die 4 "native build receipt is missing"
    native_controller_digest=""
    native_payload_digest=""
    while IFS= read -r line || [[ -n "$line" ]]; do
        count=$((count + 1))
        key="${line%%=*}"; value="${line#*=}"
        [[ "$key" != "$line" && -n "$value" ]] || die 4 "invalid native receipt"
        case "$key" in
            schema) [[ -z "$schema" ]] || die 4 "duplicate native receipt field"; schema="$value" ;;
            git_sha) [[ -z "$receipt_sha" ]] || die 4 "duplicate native receipt field"; receipt_sha="$value" ;;
            runtime_profile) [[ -z "$profile" ]] || die 4 "duplicate native receipt field"; profile="$value" ;;
            archive_sha256) [[ -z "$archive" ]] || die 4 "duplicate native receipt field"; archive="$value" ;;
            controller_sha256) [[ -z "$native_controller_digest" ]] || die 4 "duplicate native receipt field"; native_controller_digest="$value" ;;
            allowlist_sha256) [[ -z "$allowlist" ]] || die 4 "duplicate native receipt field"; allowlist="$value" ;;
            payload_sha256) [[ -z "$native_payload_digest" ]] || die 4 "duplicate native receipt field"; native_payload_digest="$value" ;;
            *) die 4 "unknown native receipt field" ;;
        esac
    done < "$receipt"
    local payload="$PROJECT_ROOT/dist/lab/$sha/miloco-native-${sha}.tar.gz"
    [[ -f "$payload" && ! -L "$payload" ]] || die 4 "native asset payload is missing"
    [[ "$count" -eq 7 && "$schema" == 2 && "$receipt_sha" == "$sha" \
        && "$profile" == "$OPENCLAW_PROFILE" && "$archive" == "$receipt_archive_digest" \
        && "$allowlist" == "$receipt_allowlist_digest" \
        && "$native_controller_digest" == "$(sha256_file "$PROJECT_ROOT/deploy/openclaw/remote-release.py")" \
        && "$native_payload_digest" == "$(sha256_file "$payload")" ]] \
        || die 4 "native receipt identity or content mismatch"
}

openclaw_remote() {
    ssh "${ssh_args[@]}" -- "$host" env "${remote_profile_env_args[@]}" \
        python3 - "$@" < "$PROJECT_ROOT/deploy/openclaw/remote-release.py"
}

openclaw_install_controller() {
    local digest="$native_controller_digest" directory="$OPENCLAW_ROOT/control/$native_controller_digest"
    # Read-only preflight has already succeeded. Only the digest-bound controller is streamed here.
    ssh "${ssh_args[@]}" -- "$host" \
        "set -euo pipefail; umask 077; for p in '$OPENCLAW_ROOT' '$OPENCLAW_ROOT/control' '$directory'; do test ! -L \"\$p\"; install -d -o root -g root -m 0700 \"\$p\"; test \"\$(stat -c '%u:%g' \"\$p\")\" = 0:0; done; temporary=\$(mktemp '$OPENCLAW_ROOT/control/.controller.XXXXXX'); trap 'rm -f -- \"\$temporary\"' EXIT; cat > \"\$temporary\"; printf '%s  %s\\n' '$digest' \"\$temporary\" | sha256sum -c - >/dev/null; chmod 0500 \"\$temporary\"; if ! ln \"\$temporary\" '$directory/remote-release.py' 2>/dev/null; then test -f '$directory/remote-release.py'; fi; test ! -L '$directory/remote-release.py'; printf '%s  %s\\n' '$digest' '$directory/remote-release.py' | sha256sum -c - >/dev/null" \
        < "$PROJECT_ROOT/deploy/openclaw/remote-release.py"
}

openclaw_dispatch() {
    if [[ "$operation" != build ]]; then
        validate_host "$host"
        [[ "$host" == "$ALLOWED_HOST_3" ]] || die 2 "OpenClaw requires the production host profile"
    fi
    openclaw_assert_clean
    if [[ "$operation" == build ]]; then
        build_release
        openclaw_build_payload "$clean_sha"
        openclaw_assert_clean
        openclaw_write_receipt "$clean_sha"
        return
    fi
    configure_ssh_identity
    configure_remote_profile_env
    case "$operation" in
        preflight|verify|status) openclaw_remote "$operation" "$host" ;;
        rollback) openclaw_remote rollback "$host" "$rollback_sha" ;;
        deploy)
            local failure_policy="${MILOCO_OPENCLAW_FAILURE_POLICY:-rollback}"
            [[ "$failure_policy" == rollback || "$failure_policy" == retain ]] \
                || die 2 "invalid native failure policy"
            openclaw_read_receipt "$clean_sha"
            openclaw_remote preflight "$host"
            openclaw_install_controller
            ssh "${ssh_args[@]}" -- "$host" env "${remote_profile_env_args[@]}" \
                python3 "$OPENCLAW_ROOT/control/$native_controller_digest/remote-release.py" \
                transaction "$host" "$clean_sha" "$native_payload_digest" \
                "$native_controller_digest" "$receipt_allowlist_digest" "$failure_policy" \
                < "$PROJECT_ROOT/dist/lab/$clean_sha/miloco-native-${clean_sha}.tar.gz"
            ;;
    esac
}
