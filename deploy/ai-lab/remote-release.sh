#!/usr/bin/env bash

set -euo pipefail

readonly LAB_ROOT="/opt/miloco-lab"
readonly RELEASES_DIR="$LAB_ROOT/releases"
readonly STATE_DIR="$LAB_ROOT/state"
readonly DEPLOY_STATE_DIR="$LAB_ROOT/deploy-state"
readonly CURRENT_FILE="$DEPLOY_STATE_DIR/current"
readonly PREVIOUS_FILE="$DEPLOY_STATE_DIR/previous"
readonly INCOMING_DIR="$LAB_ROOT/incoming"
readonly CONTROL_DIR="$LAB_ROOT/control"
readonly ARTIFACT_RECORDS_DIR="$DEPLOY_STATE_DIR/artifacts"
readonly ACCEPTED_DIR="$DEPLOY_STATE_DIR/accepted"
readonly TRANSITION_LOCK_FILE="$DEPLOY_STATE_DIR/transition.lock"
readonly REMOTE_ALLOWLIST_SHA256="02852c989db9f4efd27d1df7e3872f60af982175eb7e1e9f4bf9b751f1754ddd"
readonly HEALTH_TIMEOUT_SECONDS=120
readonly MINIMUM_DISK_KIB=5242880
readonly ROLLBACK_FAILED_EXIT_CODE=70
readonly ZERO_SHA="0000000000000000000000000000000000000000"

transition_host=""
transition_candidate=""
transition_previous=""
candidate_cleanup_sha=""

log() {
    printf '[remote-release] %s\n' "$*" >&2
}

die() {
    local exit_code="$1"
    shift
    log "ERROR: $*"
    exit "$exit_code"
}

validate_host() {
    case "$1" in
        ai-lab01.esxi|ai-lab02.esxi) ;;
        *) die 2 "host is not an approved AI-lab target" ;;
    esac
}

validate_sha() {
    [[ "$1" =~ ^[0-9a-f]{40}$ ]] || die 2 "invalid release SHA"
}

require_root() {
    [[ "$(id -u)" == "0" ]] || die 3 "remote release operations require root"
}

host_limits() {
    case "$1" in
        ai-lab01.esxi) cpu_limit="3.0"; memory_limit="3072m" ;;
        ai-lab02.esxi) cpu_limit="1.25"; memory_limit="1536m" ;;
        *) die 2 "host has no resource profile" ;;
    esac
}

compose_command() {
    local host="$1" sha="$2" timeout_seconds="$3"
    shift 3
    host_limits "$host"
    [[ "$timeout_seconds" =~ ^[1-9][0-9]*$ ]] || return 2
    local -a compose_args=()
    if [[ "$sha" == "$ZERO_SHA" && "${1:-}" == "version" ]]; then
        compose_args=("$@")
    else
        validate_sha "$sha"
        compose_args=(-p miloco-lab -f "$RELEASES_DIR/$sha/compose.yaml" "$@")
    fi
    MILOCO_RELEASE_SHA="$sha" \
    MILOCO_CPU_LIMIT="$cpu_limit" \
    MILOCO_MEMORY_LIMIT="$memory_limit" \
        timeout --signal=KILL "${timeout_seconds}s" \
        docker compose "${compose_args[@]}"
}

docker_command() {
    local timeout_seconds="$1"
    shift
    [[ "$timeout_seconds" =~ ^[1-9][0-9]*$ ]] || return 2
    timeout --signal=KILL "${timeout_seconds}s" docker "$@"
}

version_at_least() {
    local actual="$1" minimum_major="$2" minimum_minor="$3"
    [[ "$actual" =~ ^v?([0-9]+)\.([0-9]+)(\.[0-9]+)? ]] || return 1
    local major="${BASH_REMATCH[1]}" minor="${BASH_REMATCH[2]}"
    (( major > minimum_major || (major == minimum_major && minor >= minimum_minor) ))
}

preflight_release() {
    local host="$1"
    require_root
    validate_host "$host"
    [[ ! -L "$LAB_ROOT" ]] || die 4 "$LAB_ROOT must not be a symlink"
    [[ ! -L "$RELEASES_DIR" && ! -L "$STATE_DIR" && ! -L "$DEPLOY_STATE_DIR" ]] || die 4 "lab subdirectories must not be symlinks"
    [[ "$(uname -s)" == "Linux" && "$(uname -m)" == "x86_64" ]] || die 4 "platform must be linux/amd64"

    local docker_version compose_version
    docker_version="$(docker_command 15 version --format '{{.Server.Version}}')" || die 4 "Docker >= 26 is required"
    compose_version="$(compose_command "$host" "$ZERO_SHA" 15 version --short)" || die 4 "Compose >= 2.26 is required"
    version_at_least "$docker_version" 26 0 || die 4 "Docker >= 26 is required"
    version_at_least "$compose_version" 2 26 || die 4 "Compose >= 2.26 is required"

    local disk_kib
    disk_kib="$(df -Pk /opt | awk 'NR==2 {print $4}')"
    [[ "$disk_kib" =~ ^[0-9]+$ ]] || die 4 "cannot determine free disk"
    (( disk_kib >= MINIMUM_DISK_KIB )) || die 4 "at least 5 GiB free disk is required"

    host_limits "$host"
    local required_memory_kib available_memory_kib key value unit
    case "$memory_limit" in
        3072m) required_memory_kib=3145728 ;;
        1536m) required_memory_kib=1572864 ;;
        *) die 4 "invalid memory profile" ;;
    esac
    available_memory_kib=0
    while read -r key value unit; do
        if [[ "$key" == "MemAvailable:" && "$value" =~ ^[0-9]+$ && "$unit" == "kB" ]]; then
            available_memory_kib="$value"
            break
        fi
    done < /proc/meminfo
    (( available_memory_kib >= required_memory_kib )) || die 4 "insufficient available memory"

    local listeners current_sha container_id image_name container_pids
    local remaining listener_pid container_pid listener_matches
    listeners="$(ss -H -ltnp 'sport = :1810' 2>/dev/null || true)"
    if [[ -n "$listeners" ]]; then
        [[ -f "$CURRENT_FILE" && ! -L "$CURRENT_FILE" ]] || die 4 "port 1810 is owned by an unrelated listener"
        require_safe_record "$CURRENT_FILE" || die 4 "current state record is unsafe"
        current_sha="$(<"$CURRENT_FILE")"
        validate_sha "$current_sha"
        container_id="$(compose_command "$host" "$current_sha" 15 ps -q miloco 2>/dev/null || true)"
        [[ -n "$container_id" ]] || die 4 "port 1810 is owned by an unrelated listener"
        image_name="$(docker_command 10 inspect --format '{{.Config.Image}}' "$container_id" 2>/dev/null || true)"
        [[ "$image_name" == "miloco-lab:$current_sha" ]] || die 4 "port 1810 is owned by an unrelated listener"
        container_pids="$(docker_command 10 top "$container_id" -eo pid 2>/dev/null || true)"
        remaining="$listeners"
        listener_matches=0
        while [[ "$remaining" =~ pid=([0-9]+) ]]; do
            listener_pid="${BASH_REMATCH[1]}"
            listener_matches=$((listener_matches + 1))
            local owned_by_current=0
            while read -r container_pid; do
                [[ "$container_pid" == "PID" ]] && continue
                if [[ "$container_pid" == "$listener_pid" ]]; then
                    owned_by_current=1
                    break
                fi
            done <<< "$container_pids"
            (( owned_by_current == 1 )) || die 4 "port 1810 is owned by an unrelated listener"
            remaining="${remaining#*pid=$listener_pid}"
        done
        (( listener_matches > 0 )) || die 4 "port 1810 listener ownership is unavailable"
    fi
    printf 'preflight_ok host=%s platform=linux/amd64 docker=%s compose=%s port=1810 disk_kib=%s\n' \
        "$host" "$docker_version" "$compose_version" "$disk_kib"
}

verify_release_tree() {
    local release="$1" sha="$2"
    [[ -d "$release" && ! -L "$release" ]] || die 4 "unknown release"
    [[ "$(stat -c '%u:%g' "$release")" == "0:0" ]] || die 4 "release must be root owned"
    [[ -z "$(find "$release" -xdev -type l -print -quit)" ]] || die 4 "release symlinks are forbidden"
    [[ -z "$(find "$release" -xdev ! -type d ! -type f -print -quit)" ]] || die 4 "release special files are forbidden"
    [[ -z "$(find "$release" -xdev ! -user root -print -quit)" ]] || die 4 "release content must be root owned"
    [[ -f "$release/release.json" && -f "$release/SHA256SUMS" ]] || die 4 "release metadata is missing"
    grep -Eq '"schema"[[:space:]]*:[[:space:]]*1([,[:space:]]|$)' "$release/release.json" || die 4 "unsupported release schema"
    grep -Fq "\"git_sha\": \"$sha\"" "$release/release.json" || die 4 "release SHA mismatch"
    grep -Fq '"platform": "linux/amd64"' "$release/release.json" || die 4 "release platform mismatch"

    local line digest path file relative
    local -a checksummed_paths=()
    while IFS= read -r line || [[ -n "$line" ]]; do
        digest="${line%% *}"
        path="${line#*  }"
        [[ "$digest" =~ ^[0-9a-f]{64}$ && "$path" != "$line" ]] || die 4 "invalid SHA256SUMS entry"
        case "$path" in
            /*|../*|*/../*|*/..|.|..|*\\*) die 4 "checksum path escapes release" ;;
        esac
        [[ -f "$release/$path" && ! -L "$release/$path" ]] || die 4 "checksummed file is missing"
        checksummed_paths+=("$path")
    done < "$release/SHA256SUMS"
    while IFS= read -r -d '' file; do
        relative="${file#"$release"/}"
        [[ "$relative" == "SHA256SUMS" ]] && continue
        remote_path_is_allowlisted "$relative" || die 4 "release contains a non-allowlisted file"
        local checksum_match=0 checksummed_path
        for checksummed_path in "${checksummed_paths[@]}"; do
            if [[ "$checksummed_path" == "$relative" ]]; then
                checksum_match=1
                break
            fi
        done
        (( checksum_match == 1 )) || die 4 "release contains an unchecksummed file"
    done < <(find "$release" -xdev -type f -print0)
    (
        cd "$release"
        sha256sum -c "SHA256SUMS"
    ) >/dev/null || die 4 "release checksum mismatch"
}

remote_path_is_allowlisted() {
    case "$1" in
        Dockerfile|compose.yaml|container-entrypoint.sh|remote-release.sh|release.json|SHA256SUMS) ;;
        requirements/backend.txt|requirements/cli.txt|requirements/acceptance.txt) ;;
        acceptance/*) ;;
        wheels/miloco-*.whl|wheels/miloco_cli-*.whl|wheels/miloco_miot-*-manylinux_2_28_x86_64.whl) ;;
        models/miloco-models-*.tar.gz) ;;
        *) return 1 ;;
    esac
}

verify_release() {
    local sha="$1"
    validate_sha "$sha"
    local release="$RELEASES_DIR/$sha"
    [[ "$release" == "/opt/miloco-lab/releases/$sha" ]] || die 4 "release path mismatch"
    [[ ! -L "$LAB_ROOT" && ! -L "$RELEASES_DIR" && ! -L "$STATE_DIR" && ! -L "$DEPLOY_STATE_DIR" ]] || die 4 "lab paths must not be symlinks"
    require_safe_directory "$LAB_ROOT" || die 4 "lab root is unsafe"
    require_safe_directory "$RELEASES_DIR" || die 4 "release parent is unsafe"
    require_safe_directory "$release" || die 4 "release directory is unsafe"
    verify_release_tree "$release" "$sha"
}

verify_archive_digest() {
    local archive="$1" expected_digest="$2" actual_digest
    [[ "$expected_digest" =~ ^[0-9a-f]{64}$ ]] || die 2 "invalid archive digest"
    actual_digest="$(sha256sum "$archive")"
    actual_digest="${actual_digest%% *}"
    [[ "$actual_digest" == "$expected_digest" ]] || die 4 "archive SHA-256 mismatch"
}

archive_type_label() {
    case "$1" in
        p) printf 'fifo\n' ;;
        b|c) printf 'device\n' ;;
        s) printf 'socket\n' ;;
        h) printf 'hardlink\n' ;;
        l) printf 'symlink\n' ;;
        *) printf 'unsupported\n' ;;
    esac
}

validate_archive_members() {
    local archive="$1" line member normalized type label
    local type_count=0 member_count=0
    local -a seen_members=()
    while IFS= read -r line || [[ -n "$line" ]]; do
        type="${line:0:1}"
        type_count=$((type_count + 1))
        case "$type" in
            -|d) ;;
            *)
                label="$(archive_type_label "$type")"
                die 4 "archive $label members are forbidden"
                ;;
        esac
    done < <(tar --list --verbose --gzip --file "$archive" --quoting-style=escape)

    while IFS= read -r member || [[ -n "$member" ]]; do
        member_count=$((member_count + 1))
        [[ "$member" =~ ^\./[A-Za-z0-9._/+:-]*$ ]] || die 4 "archive member name is unsafe"
        normalized="${member#./}"
        normalized="${normalized%/}"
        if [[ -n "$normalized" ]]; then
            case "/$normalized/" in
                *"/../"*|*"/./"*|*"//"*) die 4 "archive member path escapes release" ;;
            esac
        fi
        local existing_member
        for existing_member in "${seen_members[@]}"; do
            [[ "$existing_member" != "$normalized" ]] || die 4 "archive contains duplicate members"
        done
        seen_members+=("$normalized")
    done < <(tar --list --gzip --file "$archive" --quoting-style=escape)
    (( member_count == type_count && member_count > 0 )) || die 4 "archive member listing is inconsistent"
    local required_member found_required
    for required_member in release.json SHA256SUMS remote-release.sh; do
        found_required=0
        for existing_member in "${seen_members[@]}"; do
            [[ "$existing_member" != "$required_member" ]] || found_required=1
        done
        (( found_required == 1 )) || die 4 "archive $required_member is missing"
    done
}

receive_release_locked() {
    local host="$1" sha="$2" expected_digest="$3" controller_digest="$4" allowlist_digest="$5"
    [[ "$expected_digest" =~ ^[0-9a-f]{64}$ ]] || die 2 "invalid archive digest"
    [[ "$controller_digest" =~ ^[0-9a-f]{64}$ ]] || die 2 "invalid controller digest"
    [[ "$allowlist_digest" == "$REMOTE_ALLOWLIST_SHA256" ]] || die 4 "allowlist digest mismatch"
    [[ ! -L "$LAB_ROOT" && ! -L "$RELEASES_DIR" && ! -L "$INCOMING_DIR" && ! -L "$DEPLOY_STATE_DIR" ]] || die 4 "lab paths must not be symlinks"
    install -d -o root -g root -m 0755 "$LAB_ROOT" "$RELEASES_DIR" "$DEPLOY_STATE_DIR" "$ARTIFACT_RECORDS_DIR"
    install -d -o root -g root -m 0700 "$INCOMING_DIR"
    local release="$RELEASES_DIR/$sha"

    local incoming staging published_release=""
    incoming="$(mktemp "$INCOMING_DIR/archive.${sha}.XXXXXX.tar.gz")"
    staging=""
    receive_cleanup() {
        rm -f -- "$incoming"
        [[ -z "$staging" || ! -e "$staging" ]] || rm -rf -- "$staging"
        [[ -z "$published_release" || ! -e "$published_release" ]] || rm -rf -- "$published_release"
    }
    trap receive_cleanup EXIT
    cat > "$incoming"
    chown root:root "$incoming"
    chmod 0600 "$incoming"
    verify_archive_digest "$incoming" "$expected_digest"
    validate_archive_members "$incoming"
    if [[ -e "$release" || -L "$release" ]]; then
        [[ -d "$release" && ! -L "$release" ]] || die 4 "existing release path is unsafe"
        verify_release_tree "$release" "$sha"
        read_artifact_record "$sha" || die 4 "existing artifact digest record is missing"
        [[ "$artifact_archive_digest" == "$expected_digest" \
            && "$artifact_controller_digest" == "$controller_digest" \
            && "$artifact_allowlist_digest" == "$allowlist_digest" ]] \
            || die 4 "existing release artifact receipt mismatch"
        trap - EXIT
        rm -f -- "$incoming"
        return 0
    fi

    staging="$(mktemp -d "$INCOMING_DIR/release.${sha}.XXXXXX")"
    tar --extract --gzip --file "$incoming" --directory "$staging" \
        --no-same-owner --no-same-permissions --delay-directory-restore
    find "$staging" -xdev -type d -exec chmod 0755 {} +
    find "$staging" -xdev -type f -exec chmod 0644 {} +
    chmod 0555 \
        "$staging/container-entrypoint.sh" \
        "$staging/remote-release.sh" \
        "$staging/acceptance/scripts/"*.sh
    chown -R root:root "$staging"
    verify_release_tree "$staging" "$sha"
    [[ ! -e "$release" && ! -L "$release" ]] || die 4 "release already exists"
    published_release="$release"
    mv -- "$staging" "$release"
    staging=""
    atomic_write "$ARTIFACT_RECORDS_DIR/$sha" \
        "schema=1
git_sha=$sha
archive_sha256=$expected_digest
controller_sha256=$controller_digest
allowlist_sha256=$allowlist_digest"
    published_release=""
    trap - EXIT
    rm -f -- "$incoming"
}

atomic_write() {
    local destination="$1" value="$2"
    local parent basename temporary
    parent="${destination%/*}"
    basename="${destination##*/}"
    [[ "$parent" != "$destination" && "$basename" =~ ^[A-Za-z0-9._-]+$ ]] || return 1
    require_safe_directory "$parent" || return 1
    [[ ! -L "$destination" ]] || return 1
    if [[ -e "$destination" ]]; then
        require_safe_record "$destination" || return 1
    fi
    temporary="$(mktemp "$parent/.${basename}.XXXXXX")" || return 1
    if ! printf '%s\n' "$value" > "$temporary"; then
        rm -f -- "$temporary"
        return 1
    fi
    if ! chown root:root "$temporary" || ! chmod 0644 "$temporary"; then
        rm -f -- "$temporary"
        return 1
    fi
    if ! mv -f -- "$temporary" "$destination"; then
        rm -f -- "$temporary"
        return 1
    fi
    require_safe_record "$destination"
}

acquire_transition_lock() {
    prepare_transaction_paths
    [[ ! -L "$TRANSITION_LOCK_FILE" ]] || die 4 "deployment lock path must not be a symlink"
    exec 9> "$TRANSITION_LOCK_FILE"
    chown root:root "$TRANSITION_LOCK_FILE"
    chmod 0600 "$TRANSITION_LOCK_FILE"
    require_safe_record "$TRANSITION_LOCK_FILE" || die 4 "deployment lock is unsafe"
    flock -n 9 || die 6 "deployment transition is locked"
}

verify_controller_self() {
    local expected_digest="$1" expected_path actual_digest actual_path
    [[ "$expected_digest" =~ ^[0-9a-f]{64}$ ]] || die 2 "invalid controller digest"
    expected_path="$CONTROL_DIR/$expected_digest/remote-release.sh"
    require_safe_directory "$CONTROL_DIR" || die 4 "control directory is unsafe"
    require_safe_directory "$CONTROL_DIR/$expected_digest" || die 4 "controller digest directory is unsafe"
    actual_path="$(realpath -e "$0")" || die 4 "controller path cannot be resolved"
    [[ "$actual_path" == "$expected_path" && -f "$actual_path" && ! -L "$actual_path" ]] \
        || die 4 "controller path is not digest-addressed"
    [[ "$(stat -c '%u:%g' "$actual_path")" == "0:0" ]] || die 4 "controller must be root owned"
    actual_digest="$(sha256sum "$actual_path")"
    actual_digest="${actual_digest%% *}"
    [[ "$actual_digest" == "$expected_digest" ]] || die 4 "controller digest mismatch"
}

transaction_release() {
    local host="$1" sha="$2" archive_digest="$3" controller_digest="$4" allowlist_digest="$5"
    require_root
    validate_host "$host"
    validate_sha "$sha"
    [[ "$archive_digest" =~ ^[0-9a-f]{64}$ \
        && "$controller_digest" =~ ^[0-9a-f]{64}$ \
        && "$allowlist_digest" =~ ^[0-9a-f]{64}$ ]] || die 2 "invalid transaction digest"
    acquire_transition_lock
    verify_controller_self "$controller_digest"
    receive_release_locked "$host" "$sha" "$archive_digest" "$controller_digest" "$allowlist_digest"
    build_and_activate_locked "$host" "$sha"
}

compose_up() {
    local host="$1" sha="$2"
    compose_command "$host" "$sha" 60 up -d --no-build --force-recreate
}

compose_container_id() {
    local host="$1" sha="$2" remaining="$3" container_id
    container_id="$(compose_command "$host" "$sha" "$remaining" ps -q miloco 2>/dev/null)" || return 2
    [[ -n "$container_id" ]] || return 1
    [[ "$container_id" =~ ^[0-9a-f]{12,64}$ ]] || return 2
    printf '%s\n' "$container_id"
}

container_health_status() {
    local container_id="$1" remaining="$2" health
    [[ "$container_id" =~ ^[0-9a-f]{12,64}$ ]] || {
        printf 'unknown\n'
        return 0
    }
    health="$(docker_command "$remaining" inspect --format '{{.State.Health.Status}}' "$container_id" 2>/dev/null || true)"
    case "$health" in
        healthy|unhealthy|starting) printf '%s\n' "$health" ;;
        *) printf 'unknown\n' ;;
    esac
}

probe_http_status() {
    local remaining="$1" probe_timeout http_status
    [[ "$remaining" =~ ^[1-9][0-9]*$ ]] || {
        printf '000\n'
        return 0
    }
    probe_timeout="$remaining"
    (( probe_timeout <= 3 )) || probe_timeout=3
    http_status="$(timeout --signal=KILL "${remaining}s" \
        curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
        --connect-timeout "$probe_timeout" --max-time "$probe_timeout" \
        http://127.0.0.1:1810/health 2>/dev/null || true)"
    [[ "$http_status" =~ ^[0-9]{3}$ ]] || http_status="000"
    printf '%s\n' "$http_status"
}

wait_for_health() {
    local host="$1" sha="$2"
    local deadline=$((SECONDS + HEALTH_TIMEOUT_SECONDS)) remaining container_id health_status http_status sleep_for
    while (( SECONDS < deadline )); do
        remaining=$((deadline - SECONDS))
        (( remaining > 0 )) || break
        container_id="$(compose_container_id "$host" "$sha" "$remaining" || true)"
        health_status="unknown"
        http_status="000"
        if [[ -n "$container_id" ]]; then
            remaining=$((deadline - SECONDS))
            (( remaining > 0 )) || break
            health_status="$(container_health_status "$container_id" "$remaining")"
            remaining=$((deadline - SECONDS))
            (( remaining > 0 )) || break
            http_status="$(probe_http_status "$remaining")"
            [[ "$health_status" == "healthy" && "$http_status" == "200" ]] && return 0
        fi
        remaining=$((deadline - SECONDS))
        (( remaining > 0 )) || break
        sleep_for=2
        (( sleep_for <= remaining )) || sleep_for="$remaining"
        sleep "$sleep_for"
    done
    return 1
}

collect_failure_evidence() {
    local host="$1" sha="$2" container_id health_status http_status container_present
    container_id="$(compose_container_id "$host" "$sha" 5 || true)"
    container_present="false"
    health_status="unknown"
    if [[ "$container_id" =~ ^[0-9a-f]{12,64}$ ]]; then
        container_present="true"
        health_status="$(container_health_status "$container_id" 5)"
    fi
    case "$health_status" in
        healthy|unhealthy|starting) ;;
        *) health_status="unknown" ;;
    esac
    http_status="$(probe_http_status 5)"
    [[ "$http_status" =~ ^[0-9]{3}$ ]] || http_status="000"
    printf 'candidate_status container_present=%s health=%s http=%s\n' \
        "$container_present" "$health_status" "$http_status" >&2
}

require_safe_directory() {
    local directory="$1" lab_real directory_real
    [[ -d "$directory" && ! -L "$directory" ]] || return 1
    [[ "$(stat -c '%u:%g' "$directory")" == "0:0" ]] || return 1
    lab_real="$(realpath -e "$LAB_ROOT")" || return 1
    directory_real="$(realpath -e "$directory")" || return 1
    case "$directory_real" in
        "$lab_real"|"$lab_real"/*) ;;
        *) return 1 ;;
    esac
}

ensure_safe_directory() {
    local directory="$1" mode="$2" owner="$3" group="$4"
    case "$directory" in
        "$LAB_ROOT"|"$LAB_ROOT"/*) ;;
        *) die 4 "directory escapes lab root" ;;
    esac
    [[ ! -L "$directory" ]] || die 4 "lab child symlink is forbidden"
    if [[ ! -e "$directory" ]]; then
        install -d -o "$owner" -g "$group" -m "$mode" "$directory"
    fi
    require_safe_directory "$directory" || die 4 "lab directory is not root-owned and contained"
}

prepare_transaction_paths() {
    local child
    for child in "$LAB_ROOT" "$RELEASES_DIR" "$DEPLOY_STATE_DIR" "$INCOMING_DIR" \
        "$ARTIFACT_RECORDS_DIR" "$ACCEPTED_DIR"; do
        [[ ! -L "$child" ]] || die 4 "lab child symlink is forbidden"
    done
    ensure_safe_directory "$LAB_ROOT" 0755 root root
    ensure_safe_directory "$RELEASES_DIR" 0755 root root
    ensure_safe_directory "$DEPLOY_STATE_DIR" 0755 root root
    ensure_safe_directory "$INCOMING_DIR" 0700 root root
    ensure_safe_directory "$ARTIFACT_RECORDS_DIR" 0755 root root
    ensure_safe_directory "$ACCEPTED_DIR" 0755 root root
}

require_safe_record() {
    local record="$1" parent basename lab_real record_real
    parent="${record%/*}"
    basename="${record##*/}"
    [[ "$parent" != "$record" && "$basename" =~ ^[A-Za-z0-9._-]+$ ]] || return 1
    require_safe_directory "$parent" || return 1
    [[ -f "$record" && ! -L "$record" ]] || return 1
    [[ "$(stat -c '%u:%g' "$record")" == "0:0" ]] || return 1
    lab_real="$(realpath -e "$LAB_ROOT")" || return 1
    record_real="$(realpath -e "$record")" || return 1
    case "$record_real" in
        "$lab_real"/*) ;;
        *) return 1 ;;
    esac
}

read_artifact_record() {
    local sha="$1" record="$ARTIFACT_RECORDS_DIR/$sha" line key value count=0
    artifact_archive_digest=""
    artifact_controller_digest=""
    artifact_allowlist_digest=""
    artifact_record_sha=""
    artifact_record_schema=""
    require_safe_directory "$DEPLOY_STATE_DIR" || return 1
    require_safe_directory "$ARTIFACT_RECORDS_DIR" || return 1
    require_safe_record "$record" || return 1
    while IFS= read -r line || [[ -n "$line" ]]; do
        count=$((count + 1))
        key="${line%%=*}"
        value="${line#*=}"
        [[ "$key" != "$line" && -n "$value" ]] || return 1
        case "$key" in
            schema) [[ -z "$artifact_record_schema" ]] || return 1; artifact_record_schema="$value" ;;
            git_sha) [[ -z "$artifact_record_sha" ]] || return 1; artifact_record_sha="$value" ;;
            archive_sha256) [[ -z "$artifact_archive_digest" ]] || return 1; artifact_archive_digest="$value" ;;
            controller_sha256) [[ -z "$artifact_controller_digest" ]] || return 1; artifact_controller_digest="$value" ;;
            allowlist_sha256) [[ -z "$artifact_allowlist_digest" ]] || return 1; artifact_allowlist_digest="$value" ;;
            *) return 1 ;;
        esac
    done < "$record"
    [[ "$count" -eq 5 && "$artifact_record_schema" == "1" && "$artifact_record_sha" == "$sha" \
        && "$artifact_archive_digest" =~ ^[0-9a-f]{64}$ \
        && "$artifact_controller_digest" =~ ^[0-9a-f]{64}$ \
        && "$artifact_allowlist_digest" == "$REMOTE_ALLOWLIST_SHA256" ]]
}

artifact_digest_for() {
    read_artifact_record "$1" || return 1
    printf '%s\n' "$artifact_archive_digest"
}

read_acceptance_marker() {
    local sha="$1" marker="$ACCEPTED_DIR/$sha" line key value count=0
    marker_schema=""
    marker_archive_digest=""
    marker_runtime_image_id=""
    marker_acceptance_image_id=""
    require_safe_directory "$DEPLOY_STATE_DIR" || return 1
    require_safe_directory "$ACCEPTED_DIR" || return 1
    require_safe_record "$marker" || return 1
    while IFS= read -r line || [[ -n "$line" ]]; do
        count=$((count + 1))
        key="${line%%=*}"
        value="${line#*=}"
        [[ "$key" != "$line" && -n "$value" ]] || return 1
        case "$key" in
            schema) [[ -z "$marker_schema" ]] || return 1; marker_schema="$value" ;;
            archive_sha256) [[ -z "$marker_archive_digest" ]] || return 1; marker_archive_digest="$value" ;;
            runtime_image_id) [[ -z "$marker_runtime_image_id" ]] || return 1; marker_runtime_image_id="$value" ;;
            acceptance_image_id) [[ -z "$marker_acceptance_image_id" ]] || return 1; marker_acceptance_image_id="$value" ;;
            *) return 1 ;;
        esac
    done < "$marker"
    [[ "$count" -eq 4 && "$marker_schema" == "1" \
        && "$marker_archive_digest" =~ ^[0-9a-f]{64}$ \
        && "$marker_runtime_image_id" =~ ^sha256:[0-9a-f]{64}$ \
        && "$marker_acceptance_image_id" =~ ^sha256:[0-9a-f]{64}$ ]]
}

docker_image_id() {
    local image="$1" image_id
    image_id="$(docker_command 15 image inspect --format '{{.Id}}' "$image" 2>/dev/null)" || return 2
    [[ "$image_id" =~ ^sha256:[0-9a-f]{64}$ ]] || return 2
    printf '%s\n' "$image_id"
}

image_reference_state() {
    local image="$1" listed image_id
    if ! listed="$(docker_command 15 image ls --quiet --no-trunc "$image" 2>/dev/null)"; then
        printf 'probe_error\n'
        return 0
    fi
    if [[ -z "$listed" ]]; then
        printf 'absent\n'
        return 0
    fi
    if ! image_id="$(docker_command 15 image inspect --format '{{.Id}}' "$image" 2>/dev/null)"; then
        printf 'probe_error\n'
        return 0
    fi
    if [[ ! "$image_id" =~ ^sha256:[0-9a-f]{64}$ ]]; then
        printf 'probe_error\n'
        return 0
    fi
    printf 'present:%s\n' "$image_id"
}

release_contract_state() {
    local sha="$1" verify_status
    if (verify_release "$sha") >/dev/null 2>&1; then
        printf 'valid\n'
        return 0
    else
        verify_status="$?"
    fi
    if (( verify_status == 4 )); then
        printf 'definitively_invalid\n'
    else
        printf 'probe_error\n'
    fi
}

mark_acceptance_success() {
    local sha="$1" runtime_image_id="$2" acceptance_image_id="$3"
    read_artifact_record "$sha" || return 1
    [[ "$runtime_image_id" =~ ^sha256:[0-9a-f]{64}$ \
        && "$acceptance_image_id" =~ ^sha256:[0-9a-f]{64}$ ]] || return 1
    atomic_write "$ACCEPTED_DIR/$sha" \
        "schema=1
archive_sha256=$artifact_archive_digest
runtime_image_id=$runtime_image_id
acceptance_image_id=$acceptance_image_id"
}

invalidate_acceptance() {
    local sha="$1" marker="$ACCEPTED_DIR/$sha"
    [[ ! -L "$ACCEPTED_DIR" && ! -L "$marker" ]] || return 1
    if [[ -e "$marker" ]]; then
        require_safe_record "$marker" || return 1
        rm -f -- "$marker" || return 1
    fi
    [[ ! -e "$marker" && ! -L "$marker" ]]
}

image_tag_absent() {
    local image="$1" listed
    listed="$(docker_command 15 image ls --quiet --no-trunc "$image" 2>/dev/null)" || return 1
    [[ -z "$listed" ]]
}

remove_image_references() {
    local runtime_image="$1" acceptance_image="$2"
    if ! docker_command 30 image rm "$runtime_image" "$acceptance_image" >/dev/null 2>&1; then
        image_tag_absent "$runtime_image" || return 1
        image_tag_absent "$acceptance_image" || return 1
        return 0
    fi
    image_tag_absent "$runtime_image" || return 1
    image_tag_absent "$acceptance_image" || return 1
}

remove_image_tags() {
    local sha="$1"
    remove_image_references "miloco-lab:$sha" "miloco-lab-acceptance:$sha"
}

remove_candidate_image_tags() {
    local sha="$1"
    remove_image_references "miloco-lab-candidate:$sha" "miloco-lab-acceptance-candidate:$sha"
}

protected_release_status() {
    local sha="$1" record protected_sha
    for record in "$CURRENT_FILE" "$PREVIOUS_FILE"; do
        if [[ -e "$record" || -L "$record" ]]; then
            require_safe_record "$record" || return 2
            protected_sha="$(<"$record")"
            [[ "$protected_sha" =~ ^[0-9a-f]{40}$ ]] || return 2
            [[ "$protected_sha" != "$sha" ]] || return 0
        fi
    done
    return 1
}

cleanup_unaccepted_candidate() {
    local sha="$1" failed=0
    invalidate_acceptance "$sha" || failed=1
    remove_image_tags "$sha" || failed=1
    remove_candidate_image_tags "$sha" || failed=1
    (( failed == 0 ))
}

candidate_build_exit() {
    local original_exit="$?" cleanup_status
    trap - EXIT
    trap '' HUP INT TERM
    set +e
    cleanup_unaccepted_candidate "$candidate_cleanup_sha"
    cleanup_status="$?"
    set -e
    if (( cleanup_status != 0 )); then
        log "build_cleanup_failed sha=$candidate_cleanup_sha"
        exit "$ROLLBACK_FAILED_EXIT_CODE"
    fi
    exit "$original_exit"
}

arm_candidate_cleanup() {
    candidate_cleanup_sha="$1"
    trap candidate_build_exit EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
}

disarm_candidate_cleanup() {
    trap - EXIT HUP INT TERM
    candidate_cleanup_sha=""
}

abort_candidate_build() {
    local sha="$1"
    trap - EXIT
    trap '' HUP INT TERM
    if ! cleanup_unaccepted_candidate "$sha"; then
        log "build_cleanup_failed sha=$sha"
    fi
    trap - HUP INT TERM
    candidate_cleanup_sha=""
    return 1
}

release_capability() {
    local sha="$1" contract_state runtime_state acceptance_state runtime_id acceptance_id
    validate_sha "$sha"
    if ! read_artifact_record "$sha" || ! read_acceptance_marker "$sha"; then
        printf 'definitively_invalid\n'
        return 0
    fi
    if [[ "$marker_archive_digest" != "$artifact_archive_digest" ]]; then
        printf 'definitively_invalid\n'
        return 0
    fi
    contract_state="$(release_contract_state "$sha")"
    [[ "$contract_state" == "valid" ]] || {
        printf '%s\n' "$contract_state"
        return 0
    }
    runtime_state="$(image_reference_state "miloco-lab:$sha")"
    acceptance_state="$(image_reference_state "miloco-lab-acceptance:$sha")"
    case "$runtime_state:$acceptance_state" in
        absent:*|*:absent)
            printf 'definitively_invalid\n'
            return 0
            ;;
        probe_error:*|*:probe_error)
            printf 'probe_error\n'
            return 0
            ;;
        present:sha256:*:present:sha256:*) ;;
        *)
            printf 'probe_error\n'
            return 0
            ;;
    esac
    runtime_id="${runtime_state#present:}"
    acceptance_id="${acceptance_state#present:}"
    if [[ "$runtime_id" != "$marker_runtime_image_id" \
        || "$acceptance_id" != "$marker_acceptance_image_id" ]]; then
        printf 'definitively_invalid\n'
        return 0
    fi
    printf 'capable\n'
}

build_images_and_accept() {
    local host="$1" sha="$2" release="$3" runtime_image_id acceptance_image_id protected_status
    local runtime_image="miloco-lab:$sha" acceptance_image="miloco-lab-acceptance:$sha"
    local candidate_runtime="miloco-lab-candidate:$sha"
    local candidate_acceptance="miloco-lab-acceptance-candidate:$sha"
    if protected_release_status "$sha"; then
        [[ "$(release_capability "$sha")" == "capable" ]] || return 1
        return 0
    else
        protected_status="$?"
        (( protected_status == 1 )) || return 1
    fi

    arm_candidate_cleanup "$sha"
    invalidate_acceptance "$sha" || abort_candidate_build "$sha"
    remove_candidate_image_tags "$sha" || abort_candidate_build "$sha"
    docker_command 3600 build --platform linux/amd64 --target runtime -t "$candidate_runtime" "$release" \
        || abort_candidate_build "$sha"
    docker_command 3600 build --platform linux/amd64 --target acceptance -t "$candidate_acceptance" "$release" \
        || abort_candidate_build "$sha"
    docker_command 900 run --rm --network none "$candidate_acceptance" \
        || abort_candidate_build "$sha"
    runtime_image_id="$(docker_image_id "$candidate_runtime")" || abort_candidate_build "$sha"
    acceptance_image_id="$(docker_image_id "$candidate_acceptance")" || abort_candidate_build "$sha"
    remove_image_tags "$sha" || abort_candidate_build "$sha"
    docker_command 30 image tag "$candidate_runtime" "$runtime_image" \
        || abort_candidate_build "$sha"
    docker_command 30 image tag "$candidate_acceptance" "$acceptance_image" \
        || abort_candidate_build "$sha"
    [[ "$(docker_image_id "$runtime_image")" == "$runtime_image_id" \
        && "$(docker_image_id "$acceptance_image")" == "$acceptance_image_id" ]] \
        || abort_candidate_build "$sha"
    remove_candidate_image_tags "$sha" || abort_candidate_build "$sha"
    mark_acceptance_success "$sha" "$runtime_image_id" "$acceptance_image_id" \
        || abort_candidate_build "$sha"
    disarm_candidate_cleanup
}

restore_previous() {
    local host="$1" previous_sha="$2" capability
    [[ -n "$previous_sha" ]] || return 1
    validate_sha "$previous_sha"
    capability="$(release_capability "$previous_sha")"
    [[ "$capability" == "capable" ]] || return 1
    compose_up "$host" "$previous_sha" || return 1
    wait_for_health "$host" "$previous_sha" || return 1
    atomic_write "$CURRENT_FILE" "$previous_sha" || return 1
}

remove_candidate() {
    local host="$1" candidate_sha="$2" container_id
    compose_command "$host" "$candidate_sha" 30 rm --stop --force miloco || return 1
    container_id="$(compose_command "$host" "$candidate_sha" 10 ps --all -q miloco 2>/dev/null)" \
        || return 1
    [[ -z "$container_id" ]]
}

transition_exit() {
    local original_exit="$?" recovery_status=1
    trap - EXIT HUP INT TERM
    set +e
    if [[ -n "$transition_previous" ]]; then
        restore_previous "$transition_host" "$transition_previous"
        recovery_status="$?"
    else
        remove_candidate "$transition_host" "$transition_candidate"
        recovery_status="$?"
    fi
    set -e
    if (( recovery_status != 0 )); then
        log "rollback_failed candidate=$transition_candidate recovery_status=$recovery_status"
        exit "$ROLLBACK_FAILED_EXIT_CODE"
    fi
    exit "$original_exit"
}

arm_transition() {
    transition_host="$1"
    transition_candidate="$2"
    transition_previous="$3"
    trap transition_exit EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
}

commit_transition() {
    trap - EXIT HUP INT TERM
}

retain_rollback_history() {
    local current_sha="$1"
    local historical_kept=0 protected_previous="" record release candidate capability index release_records
    local -a retention_candidates=() retention_capabilities=()
    capability="$(release_capability "$current_sha")"
    [[ "$capability" == "capable" ]] || {
        log "retention_probe_error sha=$current_sha state=$capability"
        return 1
    }
    if [[ -f "$PREVIOUS_FILE" && ! -L "$PREVIOUS_FILE" ]]; then
        require_safe_record "$PREVIOUS_FILE" || {
            log "retention_probe_error sha=previous state=unsafe_record"
            return 1
        }
        protected_previous="$(<"$PREVIOUS_FILE")"
        [[ "$protected_previous" =~ ^[0-9a-f]{40}$ ]] || {
            log "retention_probe_error sha=previous state=invalid_sha"
            return 1
        }
    fi
    if [[ -n "$protected_previous" && "$protected_previous" != "$current_sha" ]]; then
        capability="$(release_capability "$protected_previous")"
        case "$capability" in
            capable) historical_kept=1 ;;
            definitively_invalid) ;;
            probe_error|*)
                log "retention_probe_error sha=$protected_previous state=$capability"
                return 1
                ;;
        esac
    fi
    release_records="$(find "$RELEASES_DIR" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' | sort -rn)" || {
        log "retention_probe_error sha=listing state=probe_error"
        return 1
    }
    while IFS= read -r record; do
        release="${record#* }"
        candidate="${release##*/}"
        [[ "$candidate" =~ ^[0-9a-f]{40}$ ]] || continue
        [[ "$release" == "$RELEASES_DIR/$candidate" && -d "$release" && ! -L "$release" ]] || continue
        [[ "$candidate" != "$current_sha" ]] || continue
        if [[ -n "$protected_previous" && "$candidate" == "$protected_previous" ]]; then
            continue
        fi
        capability="$(release_capability "$candidate")"
        case "$capability" in
            capable|definitively_invalid) ;;
            probe_error|*)
                log "retention_probe_error sha=$candidate state=$capability"
                return 1
                ;;
        esac
        retention_candidates+=("$candidate")
        retention_capabilities+=("$capability")
    done <<< "$release_records"
    for ((index = 0; index < ${#retention_candidates[@]}; index++)); do
        candidate="${retention_candidates[$index]}"
        capability="${retention_capabilities[$index]}"
        if [[ "$capability" == "definitively_invalid" ]]; then
            remove_release_pair "$candidate" || return 1
        elif (( historical_kept < 2 )); then
            historical_kept=$((historical_kept + 1))
        else
            remove_release_pair "$candidate" || return 1
        fi
    done
}

remove_release_pair() {
    local sha="$1" release record protected_sha
    validate_sha "$sha"
    for record in "$CURRENT_FILE" "$PREVIOUS_FILE"; do
        if [[ -e "$record" || -L "$record" ]]; then
            require_safe_record "$record" || {
                log "cleanup_failed sha=$sha reason=unsafe_state_record"
                return 1
            }
            protected_sha="$(<"$record")"
            [[ "$protected_sha" =~ ^[0-9a-f]{40}$ ]] || {
                log "cleanup_failed sha=$sha reason=invalid_state_record"
                return 1
            }
            if [[ "$protected_sha" == "$sha" ]]; then
                log "cleanup_failed sha=$sha reason=protected_state"
                return 1
            fi
        fi
    done
    release="$RELEASES_DIR/$sha"
    [[ "$release" == "/opt/miloco-lab/releases/$sha" ]] || die 4 "release cleanup path mismatch"
    if [[ -e "$release" || -L "$release" ]]; then
        [[ ! -L "$release" ]] || die 4 "release cleanup symlink is forbidden"
        require_safe_directory "$release" || {
            log "cleanup_failed sha=$sha reason=unsafe_release"
            return 1
        }
    fi
    for record in "$ARTIFACT_RECORDS_DIR/$sha" "$ACCEPTED_DIR/$sha"; do
        if [[ -e "$record" || -L "$record" ]]; then
            require_safe_record "$record" || {
                log "cleanup_failed sha=$sha reason=unsafe_record"
                return 1
            }
        fi
    done
    remove_image_tags "$sha" || {
        log "cleanup_failed sha=$sha reason=image_removal"
        return 1
    }
    if [[ -e "$release" ]]; then
        rm -rf -- "$release" || return 1
    fi
    [[ ! -e "$release" && ! -L "$release" ]] || {
        log "cleanup_failed sha=$sha reason=release_present"
        return 1
    }
    for record in "$ARTIFACT_RECORDS_DIR/$sha" "$ACCEPTED_DIR/$sha"; do
        if [[ -e "$record" || -L "$record" ]]; then
            require_safe_record "$record" || {
                log "cleanup_failed sha=$sha reason=unsafe_record"
                return 1
            }
            rm -f -- "$record" || return 1
        fi
        [[ ! -e "$record" && ! -L "$record" ]] || {
            log "cleanup_failed sha=$sha reason=record_present"
            return 1
        }
    done
}

activate_release() {
    local host="$1" sha="$2" capability
    validate_host "$host"
    capability="$(release_capability "$sha")"
    [[ "$capability" == "capable" ]] || die 4 "release is not verified and acceptance-approved"
    [[ ! -L "$DEPLOY_STATE_DIR" ]] || die 4 "deployment state path must not be a symlink"
    [[ ! -L "$CURRENT_FILE" && ! -L "$PREVIOUS_FILE" ]] || die 4 "deployment state record is unsafe"
    install -d -o root -g root -m 0755 "$DEPLOY_STATE_DIR"

    local previous_sha=""
    if [[ -f "$CURRENT_FILE" && ! -L "$CURRENT_FILE" ]]; then
        require_safe_record "$CURRENT_FILE" || die 4 "current state record is unsafe"
        previous_sha="$(<"$CURRENT_FILE")"
        validate_sha "$previous_sha"
        atomic_write "$PREVIOUS_FILE" "$previous_sha"
    fi

    arm_transition "$host" "$sha" "$previous_sha"
    if ! compose_up "$host" "$sha" || ! wait_for_health "$host" "$sha"; then
        collect_failure_evidence "$host" "$sha"
        return 1
    fi
    atomic_write "$CURRENT_FILE" "$sha"
    commit_transition
    if ! retain_rollback_history "$sha"; then
        log "activated_cleanup_failed sha=$sha"
    fi
    return 0
}

build_and_activate_locked() {
    local host="$1" sha="$2" release="$RELEASES_DIR/$sha"
    require_root
    validate_host "$host"
    verify_release "$sha"
    build_images_and_accept "$host" "$sha" "$release"
    [[ ! -L "$STATE_DIR" ]] || die 4 "persistent state path must not be a symlink"
    install -d -o 10001 -g 10001 -m 0700 "$STATE_DIR"
    activate_release "$host" "$sha"
}

verify_running() {
    local host="$1"
    require_root
    validate_host "$host"
    [[ -f "$CURRENT_FILE" && ! -L "$CURRENT_FILE" ]] || die 4 "not deployed"
    require_safe_record "$CURRENT_FILE" || die 4 "current state record is unsafe"
    local sha="$(<"$CURRENT_FILE")"
    validate_sha "$sha"
    [[ "$(release_capability "$sha")" == "capable" ]] || die 4 "current release is not rollback capable"
    wait_for_health "$host" "$sha" || die 5 "current release is unhealthy"
    printf 'verified host=%s sha=%s\n' "$host" "$sha"
}

status_release() {
    local host="$1"
    require_root
    validate_host "$host"
    [[ ! -L "$CURRENT_FILE" ]] || die 4 "current state record is unsafe"
    if [[ ! -f "$CURRENT_FILE" ]]; then
        printf 'not_deployed host=%s\n' "$host"
        return 0
    fi
    require_safe_record "$CURRENT_FILE" || die 4 "current state record is unsafe"
    local sha="$(<"$CURRENT_FILE")"
    validate_sha "$sha"
    local container_id image health observed_image
    container_id="$(compose_container_id "$host" "$sha" 10 || true)"
    image="not_running"
    health="not_running"
    if [[ -n "$container_id" ]]; then
        observed_image="$(docker_command 10 inspect --format '{{.Config.Image}}' "$container_id" 2>/dev/null || true)"
        if [[ "$observed_image" == "miloco-lab:$sha" ]]; then
            image="$observed_image"
        else
            image="unexpected"
        fi
        health="$(container_health_status "$container_id" 10)"
    fi
    printf 'host=%s current=%s image=%s health=%s\n' "$host" "$sha" "$image" "$health"
}

rollback_release() {
    local host="$1" sha="$2" capability
    require_root
    validate_host "$host"
    acquire_transition_lock
    validate_sha "$sha"
    capability="$(release_capability "$sha")"
    [[ "$capability" == "capable" ]] || die 4 "rollback release is not verified and acceptance-approved"
    activate_release "$host" "$sha"
}

main() {
    [[ "$#" -ge 2 ]] || die 2 "operation and host are required"
    local operation="$1" host="$2"
    shift 2
    case "$operation" in
        transaction)
            [[ "$#" -eq 4 ]] || die 2 "transaction requires SHA and three digests"
            transaction_release "$host" "$1" "$2" "$3" "$4"
            ;;
        preflight)
            [[ "$#" -eq 0 ]] || die 2 "unexpected preflight argument"
            preflight_release "$host"
            ;;
        verify)
            [[ "$#" -eq 0 ]] || die 2 "unexpected verify argument"
            verify_running "$host"
            ;;
        status)
            [[ "$#" -eq 0 ]] || die 2 "unexpected status argument"
            status_release "$host"
            ;;
        rollback)
            [[ "$#" -eq 1 ]] || die 2 "rollback requires one SHA"
            rollback_release "$host" "$1"
            ;;
        *) die 2 "unknown remote operation" ;;
    esac
}

main "$@"
