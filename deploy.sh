#!/usr/bin/env bash

set -euo pipefail

readonly ALLOWED_HOST_1="ai-lab01.esxi"
readonly ALLOWED_HOST_2="ai-lab02.esxi"
readonly REMOTE_ROOT="/opt/miloco-lab"
readonly REMOTE_RELEASES="${REMOTE_ROOT}/releases"
readonly REMOTE_CONTROL_DIR="${REMOTE_ROOT}/control"

SCRIPT_PATH="${BASH_SOURCE[0]}"
case "$SCRIPT_PATH" in
    */*) PROJECT_ROOT="${SCRIPT_PATH%/*}" ;;
    *) PROJECT_ROOT="." ;;
esac
PROJECT_ROOT="$(CDPATH= cd -- "$PROJECT_ROOT" && pwd)"

operation=""
host=""
rollback_sha=""
clean_sha=""
receipt_archive_digest=""
receipt_controller_digest=""
receipt_allowlist_digest=""
receipt_artifact_path=""
ssh_args=()

log() {
    printf '[deploy] %s\n' "$*" >&2
}

die() {
    local exit_code="$1"
    shift
    log "ERROR: $*"
    exit "$exit_code"
}

usage() {
    printf '%s\n' \
        'Usage: deploy.sh <operation> [--host HOST]' \
        'Operations: build preflight deploy verify status rollback' \
        '' \
        'Host operations accept HOST positionally or as --host HOST.' \
        'Rollback additionally requires a full 40-character Git SHA.'
}

validate_sha() {
    [[ "$1" =~ ^[0-9a-f]{40}$ ]] || die 2 "release SHA must be 40 lowercase hexadecimal characters"
}

validate_host() {
    case "$1" in
        "$ALLOWED_HOST_1"|"$ALLOWED_HOST_2") ;;
        *) die 2 "host is not an approved AI-lab target" ;;
    esac
}

configure_ssh_identity() {
    local identity="${MILOCO_SSH_IDENTITY:-}" owner mode
    [[ -n "$identity" ]] || die 2 "MILOCO_SSH_IDENTITY is required for remote operations"
    [[ "$identity" == /* ]] || die 2 "MILOCO_SSH_IDENTITY must be an absolute path"
    [[ -f "$identity" && ! -L "$identity" ]] || die 2 "MILOCO_SSH_IDENTITY must be a regular non-symlink file"

    if owner="$(stat -f '%u' "$identity" 2>/dev/null)" \
        && mode="$(stat -f '%Lp' "$identity" 2>/dev/null)"; then
        :
    else
        owner="$(stat -c '%u' "$identity")" || die 2 "cannot inspect MILOCO_SSH_IDENTITY ownership"
        mode="$(stat -c '%a' "$identity")" || die 2 "cannot inspect MILOCO_SSH_IDENTITY permissions"
    fi
    [[ "$owner" == "$(id -u)" ]] || die 2 "MILOCO_SSH_IDENTITY must be owned by the current user"
    [[ "$mode" =~ ^[0-7]{3,4}$ && $((8#$mode & 8#77)) -eq 0 ]] \
        || die 2 "MILOCO_SSH_IDENTITY must not grant group or other permissions"
    ssh_args=(-o BatchMode=yes -o IdentitiesOnly=yes -i "$identity")
}

parse_arguments() {
    [[ "$#" -gt 0 ]] || {
        usage >&2
        exit 2
    }
    case "$1" in
        -h|--help)
            usage
            exit 0
            ;;
        build|preflight|deploy|verify|status|rollback)
            operation="$1"
            shift
            ;;
        *)
            usage >&2
            exit 2
            ;;
    esac

    while [[ "$#" -gt 0 ]]; do
        case "$1" in
            --host)
                [[ "$#" -ge 2 && -z "$host" ]] || die 2 "--host requires one value"
                host="$2"
                shift 2
                ;;
            --)
                shift
                break
                ;;
            *)
                if [[ "$operation" != "build" && -z "$host" ]]; then
                    host="$1"
                elif [[ "$operation" == "rollback" && -z "$rollback_sha" ]]; then
                    rollback_sha="$1"
                else
                    die 2 "unexpected argument"
                fi
                shift
                ;;
        esac
    done
    [[ "$#" -eq 0 ]] || die 2 "unexpected argument"

    if [[ "$operation" == "build" ]]; then
        [[ -z "$host" && -z "$rollback_sha" ]] || die 2 "build does not accept a host or SHA"
    else
        [[ -n "$host" ]] || die 2 "host is required"
    fi
    if [[ "$operation" == "rollback" ]]; then
        [[ -n "$rollback_sha" ]] || die 2 "rollback requires an exact SHA"
        validate_sha "$rollback_sha"
    fi
}

assert_clean_worktree() {
    local dirty
    dirty="$(git status --porcelain --untracked-files=normal)" || die 3 "cannot inspect worktree"
    [[ -z "$dirty" ]] || die 3 "worktree must be clean"
    clean_sha="$(git rev-parse --verify HEAD)" || die 3 "cannot resolve Git HEAD"
    validate_sha "$clean_sha"
}

assert_clean_controller() {
    assert_clean_worktree
    git ls-files --error-unmatch -- \
        deploy.sh deploy/ai-lab/remote-release.sh deploy/ai-lab/artifact-files.txt >/dev/null 2>&1 \
        || die 3 "deployment controllers must be tracked"
    git diff --quiet "$clean_sha" -- \
        deploy.sh deploy/ai-lab/remote-release.sh deploy/ai-lab/artifact-files.txt \
        || die 3 "deployment controllers must match clean Git HEAD"
}

select_one() {
    local label="$1"
    shift
    local -a matches=()
    local pattern candidate
    shopt -s nullglob
    for pattern in "$@"; do
        for candidate in $pattern; do
            [[ -f "$candidate" && ! -L "$candidate" ]] && matches+=("$candidate")
        done
    done
    shopt -u nullglob
    [[ "${#matches[@]}" -eq 1 ]] || die 4 "expected exactly one $label artifact, found ${#matches[@]}"
    printf '%s\n' "${matches[0]}"
}

acceptance_fixture_path_is_safe() {
    case "$1" in
        h264_annexb_packets.bin|h264_avcc_packets.bin|h264_video_audio.mkv|h265_video_only.mkv)
            return 0
            ;;
        *)
            return 4
            ;;
    esac
}

copy_acceptance_payload() {
    local staging="$1" fixture_root fixture relative destination
    local -a fixture_relative_paths=(
        h264_annexb_packets.bin
        h264_avcc_packets.bin
        h264_video_audio.mkv
        h265_video_only.mkv
    )
    install -d -m 0755 \
        "$staging/acceptance" \
        "$staging/acceptance/integration" \
        "$staging/acceptance/fixtures" \
        "$staging/acceptance/fixtures/rtsp" \
        "$staging/acceptance/scripts"
    install -m 0644 "$PROJECT_ROOT/deploy/ai-lab/acceptance/pytest.ini" "$staging/acceptance/pytest.ini"
    install -m 0644 \
        "$PROJECT_ROOT/backend/miloco/tests/integration/test_rtsp_perception.py" \
        "$PROJECT_ROOT/backend/miloco/tests/integration/test_rtsp_live_view.py" \
        "$PROJECT_ROOT/backend/miloco/tests/integration/test_responses_perception.py" \
        "$PROJECT_ROOT/backend/miloco/tests/integration/responses_fixture_server.py" \
        "$staging/acceptance/integration/"
    fixture_root="$PROJECT_ROOT/backend/miloco/tests/fixtures/rtsp"
    for relative in "${fixture_relative_paths[@]}"; do
        fixture="$fixture_root/$relative"
        [[ -f "$fixture" && ! -L "$fixture" ]] || die 4 "missing RTSP fixture artifact"
        acceptance_fixture_path_is_safe "$relative" \
            || die 4 "forbidden RTSP fixture path"
        destination="$staging/acceptance/fixtures/rtsp/$relative"
        install -d -m 0755 "$(dirname "$destination")"
        install -m 0644 "$fixture" "$destination"
    done
    install -m 0555 \
        "$PROJECT_ROOT/scripts/rtsp-view-smoke.sh" \
        "$PROJECT_ROOT/scripts/responses-vlm-smoke.sh" \
        "$staging/acceptance/scripts/"
}

path_is_allowlisted() {
    local relative_path="$1"
    local entry prefix suffix
    while IFS= read -r entry || [[ -n "$entry" ]]; do
        [[ -n "$entry" && "${entry:0:1}" != "#" ]] || continue
        if [[ "$relative_path" == */ && "$entry" == "$relative_path"* ]]; then
            return 0
        fi
        if [[ "$entry" == */ ]]; then
            [[ "$relative_path" == "$entry"* ]] && return 0
            continue
        fi
        if [[ "$entry" == *"*"* ]]; then
            prefix="${entry%%\**}"
            suffix="${entry#*\*}"
            [[ "$relative_path" == "$prefix"*"$suffix" ]] && return 0
            continue
        fi
        [[ "$relative_path" == "$entry" ]] && return 0
    done < "$PROJECT_ROOT/deploy/ai-lab/artifact-files.txt"
    return 1
}

release_path_is_forbidden() {
    local relative_path="$1" fixture_relative
    case "/$relative_path/" in
        */.git/*|*/.env/*|*/.env.*/*|*/config.json/*|*/credentials.json/*|\
        */.venv/*|*/venv/*|*/node_modules/*|*/__pycache__/*|*/.pytest_cache/*|\
        */.mypy_cache/*|*/.ruff_cache/*|*/site-packages/*)
            return 0
            ;;
    esac
    if [[ "$relative_path" == acceptance/fixtures/rtsp/?* ]]; then
        fixture_relative="${relative_path#acceptance/fixtures/rtsp/}"
        acceptance_fixture_path_is_safe "${fixture_relative%/}" || return 0
    fi
    return 1
}

validate_staging_allowlist() {
    local staging="$1"
    local path relative
    while IFS= read -r -d '' path; do
        relative="${path#"$staging"/}"
        [[ "$relative" != "$path" ]] || die 4 "staging path escaped release root"
        [[ ! -L "$path" ]] || die 4 "release symlinks are forbidden"
        if [[ -d "$path" ]]; then
            relative="$relative/"
        elif [[ ! -f "$path" ]]; then
            die 4 "release path must be a regular file or directory"
        fi
        release_path_is_forbidden "$relative" && die 4 "forbidden release path"
        path_is_allowlisted "$relative" || die 4 "release path is not allowlisted: $relative"
    done < <(find "$staging" -mindepth 1 -print0)
}

write_release_json() {
    local staging="$1"
    local sha="$2"
    local built_at="$3"
    local miloco_name="$4"
    local cli_name="$5"
    local miot_name="$6"
    local models_name="$7"
    printf '{\n  "schema": 1,\n  "git_sha": "%s",\n  "built_at": "%s",\n  "platform": "linux/amd64",\n  "artifacts": {\n    "miloco": "%s",\n    "cli": "%s",\n    "miot": "%s",\n    "models": "%s"\n  }\n}\n' \
        "$sha" "$built_at" "$miloco_name" "$cli_name" "$miot_name" "$models_name" \
        > "$staging/release.json"
}

generate_checksums() {
    local staging="$1"
    local checksum_file="$staging/SHA256SUMS"
    local path relative
    : > "$checksum_file"
    while IFS= read -r -d '' path; do
        relative="${path#"$staging"/}"
        [[ "$relative" != "SHA256SUMS" ]] || continue
        (
            cd "$staging"
            sha256sum "$relative"
        ) >> "$checksum_file"
    done < <(find "$staging" -type f ! -name SHA256SUMS -print0)
}

sha256_file() {
    local file="$1" output
    if output="$(shasum -a 256 -- "$file" 2>/dev/null)"; then
        printf '%s\n' "${output%% *}"
        return 0
    fi
    output="$(sha256sum -- "$file")" || die 4 "cannot calculate SHA-256"
    printf '%s\n' "${output%% *}"
}

write_build_receipt() {
    local sha="$1" archive="$2" receipt="$3" temporary_receipt="$4"
    local archive_digest controller_digest allowlist_digest artifact_path
    archive_digest="$(sha256_file "$archive")"
    controller_digest="$(sha256_file "$PROJECT_ROOT/deploy/ai-lab/remote-release.sh")"
    allowlist_digest="$(sha256_file "$PROJECT_ROOT/deploy/ai-lab/artifact-files.txt")"
    artifact_path="${archive#"$PROJECT_ROOT"/}"
    [[ "$archive_digest" =~ ^[0-9a-f]{64}$ ]] || die 4 "invalid archive receipt digest"
    [[ "$controller_digest" =~ ^[0-9a-f]{64}$ ]] || die 4 "invalid controller receipt digest"
    [[ "$allowlist_digest" =~ ^[0-9a-f]{64}$ ]] || die 4 "invalid allowlist receipt digest"
    [[ "$artifact_path" == "dist/lab/$sha/miloco-lab-${sha}.tar.gz" ]] \
        || die 4 "invalid receipt artifact path"
    printf 'schema=1\ngit_sha=%s\narchive_sha256=%s\ncontroller_sha256=%s\nallowlist_sha256=%s\nartifact_path=%s\n' \
        "$sha" "$archive_digest" "$controller_digest" "$allowlist_digest" "$artifact_path" \
        > "$temporary_receipt"
    chmod 0444 "$temporary_receipt"
    mv -f -- "$temporary_receipt" "$receipt"
}

read_release_receipt() {
    local sha="$1" receipt line key value line_count=0 schema="" receipt_sha=""
    local expected_archive expected_receipt actual_archive actual_controller actual_allowlist
    expected_archive="$PROJECT_ROOT/dist/lab/$sha/miloco-lab-${sha}.tar.gz"
    expected_receipt="$PROJECT_ROOT/dist/lab/$sha/miloco-lab-${sha}.receipt"
    [[ -f "$expected_archive" && ! -L "$expected_archive" ]] || die 4 "receipt artifact is missing"
    [[ -f "$expected_receipt" && ! -L "$expected_receipt" ]] || die 4 "build receipt is missing"
    receipt_archive_digest=""
    receipt_controller_digest=""
    receipt_allowlist_digest=""
    receipt_artifact_path=""
    while IFS= read -r line || [[ -n "$line" ]]; do
        line_count=$((line_count + 1))
        key="${line%%=*}"
        value="${line#*=}"
        [[ "$key" != "$line" && -n "$value" ]] || die 4 "invalid build receipt"
        case "$key" in
            schema) [[ -z "$schema" ]] || die 4 "duplicate build receipt field"; schema="$value" ;;
            git_sha) [[ -z "$receipt_sha" ]] || die 4 "duplicate build receipt field"; receipt_sha="$value" ;;
            archive_sha256) [[ -z "$receipt_archive_digest" ]] || die 4 "duplicate build receipt field"; receipt_archive_digest="$value" ;;
            controller_sha256) [[ -z "$receipt_controller_digest" ]] || die 4 "duplicate build receipt field"; receipt_controller_digest="$value" ;;
            allowlist_sha256) [[ -z "$receipt_allowlist_digest" ]] || die 4 "duplicate build receipt field"; receipt_allowlist_digest="$value" ;;
            artifact_path) [[ -z "$receipt_artifact_path" ]] || die 4 "duplicate build receipt field"; receipt_artifact_path="$value" ;;
            *) die 4 "unknown build receipt field" ;;
        esac
    done < "$expected_receipt"
    [[ "$line_count" -eq 6 && "$schema" == "1" && "$receipt_sha" == "$sha" ]] \
        || die 4 "build receipt identity mismatch"
    [[ "$receipt_archive_digest" =~ ^[0-9a-f]{64}$ \
        && "$receipt_controller_digest" =~ ^[0-9a-f]{64}$ \
        && "$receipt_allowlist_digest" =~ ^[0-9a-f]{64}$ ]] \
        || die 4 "build receipt digest is invalid"
    [[ "$receipt_artifact_path" == "dist/lab/$sha/miloco-lab-${sha}.tar.gz" ]] \
        || die 4 "build receipt artifact path mismatch"
    actual_archive="$(sha256_file "$expected_archive")"
    actual_controller="$(sha256_file "$PROJECT_ROOT/deploy/ai-lab/remote-release.sh")"
    actual_allowlist="$(sha256_file "$PROJECT_ROOT/deploy/ai-lab/artifact-files.txt")"
    [[ "$actual_archive" == "$receipt_archive_digest" \
        && "$actual_controller" == "$receipt_controller_digest" \
        && "$actual_allowlist" == "$receipt_allowlist_digest" ]] \
        || die 4 "build receipt content mismatch"
}

build_release() (
    local sha="$clean_sha"
    local temp_root staging output_dir archive receipt temporary_archive temporary_receipt built_at
    temp_root="$(mktemp -d)"
    temporary_archive=""
    temporary_receipt=""
    cleanup_build_staging() {
        rm -rf -- "$temp_root"
        if [[ -n "$temporary_archive" && -e "$temporary_archive" ]]; then
            rm -f -- "$temporary_archive"
        fi
        if [[ -n "$temporary_receipt" && -e "$temporary_receipt" ]]; then
            rm -f -- "$temporary_receipt"
        fi
    }
    trap cleanup_build_staging EXIT
    unset \
        MILOCO_MODEL__OMNI__API_KEY \
        MILOCO_RESPONSES_API_KEY \
        OPENAI_API_KEY \
        OMNI_API_KEY \
        RESPONSES_API_KEY \
        MILOCO_RESPONSES_BASE_URL \
        MILOCO_RESPONSES_MODEL \
        MILOCO_RTSP_TEST_URL \
        MILOCO_RTSP_TEST_USERNAME \
        MILOCO_RTSP_TEST_PASSWORD
    staging="$temp_root/release"
    install -d -m 0755 "$staging/wheels" "$staging/models" "$staging/requirements"

    cd "$PROJECT_ROOT"
    env \
        -u MILOCO_MODEL__OMNI__API_KEY \
        -u MILOCO_RESPONSES_API_KEY \
        -u OPENAI_API_KEY \
        -u OMNI_API_KEY \
        -u RESPONSES_API_KEY \
        -u MILOCO_RESPONSES_BASE_URL \
        -u MILOCO_RESPONSES_MODEL \
        -u MILOCO_RTSP_TEST_URL \
        -u MILOCO_RTSP_TEST_USERNAME \
        -u MILOCO_RTSP_TEST_PASSWORD \
        ./scripts/build.sh --packages web,miloco-miot,miloco,miloco-cli

    local miloco_wheel cli_wheel miot_wheel models_archive
    miloco_wheel="$(select_one "Miloco wheel" "$PROJECT_ROOT/dist/miloco-"*.whl)"
    cli_wheel="$(select_one "CLI wheel" "$PROJECT_ROOT/dist/miloco_cli-"*.whl)"
    miot_wheel="$(select_one "Linux x86_64 MIoT wheel" "$PROJECT_ROOT/dist/miloco_miot-"*manylinux_2_28_x86_64.whl)"
    models_archive="$(select_one "model archive" "$PROJECT_ROOT/dist/miloco-models-"*.tar.gz)"

    cp -- "$miloco_wheel" "$cli_wheel" "$miot_wheel" "$staging/wheels/"
    cp -- "$models_archive" "$staging/models/"
    cp -- \
        "$PROJECT_ROOT/deploy/ai-lab/Dockerfile" \
        "$PROJECT_ROOT/deploy/ai-lab/compose.yaml" \
        "$PROJECT_ROOT/deploy/ai-lab/container-entrypoint.sh" \
        "$PROJECT_ROOT/deploy/ai-lab/remote-release.sh" \
        "$staging/"
    copy_acceptance_payload "$staging"
    validate_staging_allowlist "$staging"

    (
        cd "$PROJECT_ROOT/backend"
        uv export --locked --no-dev --no-emit-workspace > "$staging/requirements/backend.txt"
        uv export --locked --no-emit-workspace > "$staging/requirements/acceptance.txt"
    )
    (
        cd "$PROJECT_ROOT/cli"
        uv export --locked --no-dev --no-emit-workspace > "$staging/requirements/cli.txt"
    )

    assert_clean_controller
    [[ "$clean_sha" == "$sha" ]] || die 3 "Git HEAD changed during build"

    built_at="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
    write_release_json \
        "$staging" "$sha" "$built_at" \
        "${miloco_wheel##*/}" "${cli_wheel##*/}" "${miot_wheel##*/}" "${models_archive##*/}"
    validate_staging_allowlist "$staging"
    generate_checksums "$staging"
    validate_staging_allowlist "$staging"

    output_dir="$PROJECT_ROOT/dist/lab/$sha"
    archive="$output_dir/miloco-lab-${sha}.tar.gz"
    receipt="$output_dir/miloco-lab-${sha}.receipt"
    install -d -m 0755 "$output_dir"
    temporary_archive="$(mktemp "$output_dir/.miloco-lab-${sha}.tar.gz.XXXXXX")"
    COPYFILE_DISABLE=1 tar -czf "$temporary_archive" -C "$staging" -- .
    mv -f -- "$temporary_archive" "$archive"
    temporary_archive=""
    temporary_receipt="$(mktemp "$output_dir/.miloco-lab-${sha}.receipt.XXXXXX")"
    write_build_receipt "$sha" "$archive" "$receipt" "$temporary_receipt"
    temporary_receipt=""
    printf '%s\n' "$archive"
)

local_release_archive() {
    assert_clean_worktree
    local archive="$PROJECT_ROOT/dist/lab/$clean_sha/miloco-lab-${clean_sha}.tar.gz"
    [[ -f "$archive" && ! -L "$archive" ]] || die 4 "build the exact clean SHA before remote operations"
    printf '%s\n' "$archive"
}

run_remote_controller() {
    local target_host="$1"
    shift
    ssh "${ssh_args[@]}" -- "$target_host" bash -s -- "$@" < "$PROJECT_ROOT/deploy/ai-lab/remote-release.sh"
}

preflight_remote() {
    local target_host="$1"
    run_remote_controller "$target_host" preflight "$target_host"
}

install_remote_controller() {
    local target_host="$1" controller_digest="$2"
    local controller="$PROJECT_ROOT/deploy/ai-lab/remote-release.sh"
    local controller_dir controller_path
    [[ "$controller_digest" =~ ^[0-9a-f]{64}$ ]] || die 4 "invalid controller digest"
    controller_dir="${REMOTE_CONTROL_DIR}/${controller_digest}"
    controller_path="${REMOTE_CONTROL_DIR}/${controller_digest}/remote-release.sh"
    ssh "${ssh_args[@]}" -- "$target_host" \
        "set -euo pipefail; umask 077; test ! -L '${REMOTE_ROOT}'; install -d -o root -g root -m 0755 '${REMOTE_ROOT}'; test \"\$(stat -c '%u:%g' '${REMOTE_ROOT}')\" = 0:0; test ! -L '${REMOTE_CONTROL_DIR}'; install -d -o root -g root -m 0755 '${REMOTE_CONTROL_DIR}'; test \"\$(stat -c '%u:%g' '${REMOTE_CONTROL_DIR}')\" = 0:0; test ! -L '${controller_dir}'; install -d -o root -g root -m 0555 '${controller_dir}'; temporary=\$(mktemp '${REMOTE_CONTROL_DIR}/.${controller_digest}.XXXXXX'); trap 'rm -f -- \"\$temporary\"' EXIT; cat > \"\$temporary\"; printf '%s  %s\\n' '${controller_digest}' \"\$temporary\" | sha256sum -c - >/dev/null; chown root:root \"\$temporary\"; chmod 0555 \"\$temporary\"; if ! ln \"\$temporary\" '${controller_path}' 2>/dev/null; then test -e '${controller_path}'; fi; test -f '${controller_path}'; test ! -L '${controller_path}'; test \"\$(stat -c '%u:%g' '${controller_path}')\" = 0:0; printf '%s  %s\\n' '${controller_digest}' '${controller_path}' | sha256sum -c - >/dev/null; chmod 0555 '${controller_dir}'; test \"\$(realpath -e '${controller_path}')\" = '${controller_path}'" \
        < "$controller"
}

deploy_remote() {
    local target_host="$1"
    local archive sha controller_path
    sha="$clean_sha"
    archive="$PROJECT_ROOT/dist/lab/$sha/miloco-lab-${sha}.tar.gz"
    read_release_receipt "$sha"
    controller_path="${REMOTE_CONTROL_DIR}/${receipt_controller_digest}/remote-release.sh"
    install_remote_controller "$target_host" "$receipt_controller_digest"
    ssh "${ssh_args[@]}" -- "$target_host" "$controller_path" preflight "$target_host"
    ssh "${ssh_args[@]}" -- "$target_host" "$controller_path" transaction "$target_host" "$sha" \
        "$receipt_archive_digest" "$receipt_controller_digest" "$receipt_allowlist_digest" \
        < "$archive"
}

# DISPATCH_START
dispatch() {
    case "$operation" in
        build)
            # BUILD_GUARD_START
            assert_clean_worktree
            # CLEAN_WORKTREE_VALIDATION_COMPLETE
            build_release
            ;;
        preflight|deploy|verify|status|rollback)
            # HOST_GUARD_START
            validate_host "$host"
            # HOST_VALIDATION_COMPLETE
            assert_clean_controller
            configure_ssh_identity
            case "$operation" in
                preflight) preflight_remote "$host" ;;
                deploy) deploy_remote "$host" ;;
                verify) run_remote_controller "$host" verify "$host" ;;
                status) run_remote_controller "$host" status "$host" ;;
                rollback) run_remote_controller "$host" rollback "$host" "$rollback_sha" ;;
            esac
            ;;
    esac
}

parse_arguments "$@"
cd "$PROJECT_ROOT"
dispatch
