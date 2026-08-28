#!/usr/bin/env bash

set -euo pipefail

readonly ALLOWED_HOST_1="ai-lab01.esxi"
readonly ALLOWED_HOST_2="ai-lab02.esxi"
readonly REMOTE_ROOT="/opt/miloco-lab"
readonly REMOTE_RELEASES="${REMOTE_ROOT}/releases"

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

copy_acceptance_payload() {
    local staging="$1"
    install -d -m 0755 \
        "$staging/acceptance/integration" \
        "$staging/acceptance/fixtures/rtsp" \
        "$staging/acceptance/scripts"
    cp -- "$PROJECT_ROOT/deploy/ai-lab/acceptance/pytest.ini" "$staging/acceptance/pytest.ini"
    cp -- \
        "$PROJECT_ROOT/backend/miloco/tests/integration/test_rtsp_perception.py" \
        "$PROJECT_ROOT/backend/miloco/tests/integration/test_rtsp_live_view.py" \
        "$PROJECT_ROOT/backend/miloco/tests/integration/test_responses_perception.py" \
        "$PROJECT_ROOT/backend/miloco/tests/integration/responses_fixture_server.py" \
        "$staging/acceptance/integration/"
    cp -- "$PROJECT_ROOT/backend/miloco/tests/fixtures/rtsp/"* "$staging/acceptance/fixtures/rtsp/"
    cp -- \
        "$PROJECT_ROOT/scripts/rtsp-smoke.sh" \
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
        case "/$relative/" in
            */.git/*|*/.env/*|*/config.json/*|*/.venv/*|*/node_modules/*|*/__pycache__/*)
                die 4 "forbidden release path"
                ;;
        esac
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

build_release() (
    local sha="$clean_sha"
    local temp_root staging output_dir archive temporary_archive built_at
    temp_root="$(mktemp -d)"
    temporary_archive=""
    cleanup_build_staging() {
        rm -rf -- "$temp_root"
        if [[ -n "$temporary_archive" && -e "$temporary_archive" ]]; then
            rm -f -- "$temporary_archive"
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

    (
        cd "$PROJECT_ROOT/backend"
        uv export --locked --no-dev --no-emit-workspace > "$staging/requirements/backend.txt"
        uv export --locked --no-emit-workspace > "$staging/requirements/acceptance.txt"
    )
    (
        cd "$PROJECT_ROOT/cli"
        uv export --locked --no-dev --no-emit-workspace > "$staging/requirements/cli.txt"
    )

    assert_clean_worktree
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
    install -d -m 0755 "$output_dir"
    temporary_archive="$(mktemp "$output_dir/.miloco-lab-${sha}.tar.gz.XXXXXX")"
    COPYFILE_DISABLE=1 tar -czf "$temporary_archive" -C "$staging" -- .
    mv -f -- "$temporary_archive" "$archive"
    temporary_archive=""
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
    ssh -- "$target_host" bash -s -- "$@" < "$PROJECT_ROOT/deploy/ai-lab/remote-release.sh"
}

preflight_remote() {
    local target_host="$1"
    run_remote_controller "$target_host" preflight "$target_host"
}

deploy_remote() {
    local target_host="$1"
    local archive sha remote_release
    assert_clean_worktree
    sha="$clean_sha"
    archive="$PROJECT_ROOT/dist/lab/$sha/miloco-lab-${sha}.tar.gz"
    [[ -f "$archive" && ! -L "$archive" ]] || die 4 "build the exact clean SHA before remote operations"
    remote_release="${REMOTE_RELEASES}/${sha}"
    preflight_remote "$target_host"
    ssh -- "$target_host" \
        "set -euo pipefail; umask 077; test ! -L '${REMOTE_ROOT}'; install -d -o root -g root -m 0755 '${REMOTE_ROOT}'; test ! -L '${REMOTE_RELEASES}'; install -d -o root -g root -m 0755 '${REMOTE_RELEASES}'; test ! -e '${remote_release}'; mkdir -m 0755 '${remote_release}'; trap 'rm -rf -- \"${remote_release}\"' EXIT; tar -xzf - -C '${remote_release}'; trap - EXIT" \
        < "$archive"
    run_remote_controller "$target_host" activate "$target_host" "$sha"
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
