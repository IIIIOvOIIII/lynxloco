#!/usr/bin/env bash

set -euo pipefail

readonly LAB_ROOT="/opt/miloco-lab"
readonly RELEASES_DIR="$LAB_ROOT/releases"
readonly STATE_DIR="$LAB_ROOT/state"
readonly DEPLOY_STATE_DIR="$LAB_ROOT/deploy-state"
readonly CURRENT_FILE="$DEPLOY_STATE_DIR/current"
readonly PREVIOUS_FILE="$DEPLOY_STATE_DIR/previous"
readonly HEALTH_TIMEOUT_SECONDS=120
readonly MINIMUM_DISK_KIB=5242880

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
    docker_version="$(docker version --format '{{.Server.Version}}')" || die 4 "Docker >= 26 is required"
    compose_version="$(docker compose version --short)" || die 4 "Compose >= 2.26 is required"
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
        current_sha="$(<"$CURRENT_FILE")"
        validate_sha "$current_sha"
        container_id="$(docker compose -p miloco-lab -f "$RELEASES_DIR/$current_sha/compose.yaml" ps -q miloco 2>/dev/null || true)"
        [[ -n "$container_id" ]] || die 4 "port 1810 is owned by an unrelated listener"
        image_name="$(docker inspect --format '{{.Config.Image}}' "$container_id" 2>/dev/null || true)"
        [[ "$image_name" == "miloco-lab:$current_sha" ]] || die 4 "port 1810 is owned by an unrelated listener"
        container_pids="$(docker top "$container_id" -eo pid 2>/dev/null || true)"
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

verify_release() {
    local sha="$1"
    validate_sha "$sha"
    local release="$RELEASES_DIR/$sha"
    [[ "$release" == "/opt/miloco-lab/releases/$sha" ]] || die 4 "release path mismatch"
    [[ ! -L "$LAB_ROOT" && ! -L "$RELEASES_DIR" && ! -L "$STATE_DIR" && ! -L "$DEPLOY_STATE_DIR" ]] || die 4 "lab paths must not be symlinks"
    [[ -d "$release" && ! -L "$release" ]] || die 4 "unknown release"
    [[ "$(stat -c '%u:%g' "$release")" == "0:0" ]] || die 4 "release must be root owned"
    [[ -z "$(find "$release" -xdev -type l -print -quit)" ]] || die 4 "release symlinks are forbidden"
    [[ -z "$(find "$release" -xdev ! -user root -print -quit)" ]] || die 4 "release content must be root owned"
    [[ -f "$release/release.json" && -f "$release/SHA256SUMS" ]] || die 4 "release metadata is missing"
    grep -Eq '"schema"[[:space:]]*:[[:space:]]*1([,[:space:]]|$)' "$release/release.json" || die 4 "unsupported release schema"
    grep -Fq "\"git_sha\": \"$sha\"" "$release/release.json" || die 4 "release SHA mismatch"
    grep -Fq '"platform": "linux/amd64"' "$release/release.json" || die 4 "release platform mismatch"

    local line digest path file relative
    declare -A checksummed_paths=()
    while IFS= read -r line || [[ -n "$line" ]]; do
        digest="${line%% *}"
        path="${line#*  }"
        [[ "$digest" =~ ^[0-9a-f]{64}$ && "$path" != "$line" ]] || die 4 "invalid SHA256SUMS entry"
        case "$path" in
            /*|../*|*/../*|*/..|.|..|*\\*) die 4 "checksum path escapes release" ;;
        esac
        [[ -f "$release/$path" && ! -L "$release/$path" ]] || die 4 "checksummed file is missing"
        checksummed_paths["$path"]=1
    done < "$release/SHA256SUMS"
    while IFS= read -r -d '' file; do
        relative="${file#"$release"/}"
        [[ "$relative" == "SHA256SUMS" ]] && continue
        [[ -n "${checksummed_paths[$relative]+present}" ]] || die 4 "release contains an unchecksummed file"
    done < <(find "$release" -xdev -type f -print0)
    (
        cd "$release"
        sha256sum -c "SHA256SUMS"
    ) >/dev/null
}

atomic_write() {
    local destination="$1" value="$2"
    local temporary="${destination}.tmp.$$"
    printf '%s\n' "$value" > "$temporary"
    chown root:root "$temporary"
    chmod 0644 "$temporary"
    mv -f -- "$temporary" "$destination"
}

compose_up() {
    local host="$1" sha="$2"
    host_limits "$host"
    MILOCO_RELEASE_SHA="$sha" \
    MILOCO_CPU_LIMIT="$cpu_limit" \
    MILOCO_MEMORY_LIMIT="$memory_limit" \
        docker compose -p miloco-lab -f "$RELEASES_DIR/$sha/compose.yaml" \
        up -d --no-build --force-recreate
}

wait_for_health() {
    local sha="$1"
    local deadline=$((SECONDS + HEALTH_TIMEOUT_SECONDS)) container_id health_status http_status
    while (( SECONDS < deadline )); do
        container_id="$(docker compose -p miloco-lab -f "$RELEASES_DIR/$sha/compose.yaml" ps -q miloco 2>/dev/null || true)"
        if [[ -n "$container_id" ]]; then
            health_status="$(docker inspect --format '{{.State.Health.Status}}' "$container_id" 2>/dev/null || true)"
            http_status="$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' --max-time 3 http://127.0.0.1:1810/health 2>/dev/null || true)"
            [[ "$health_status" == "healthy" && "$http_status" == "200" ]] && return 0
        fi
        sleep 2
    done
    return 1
}

sanitize_logs() {
    sed -E \
        -e '/authorization|token|password|secret|api.?key|rtsps?:/I { s/.*/[REDACTED sensitive log line]/; }'
}

collect_failure_evidence() {
    local sha="$1"
    docker compose -p miloco-lab -f "$RELEASES_DIR/$sha/compose.yaml" ps 2>&1 | sanitize_logs >&2 || true
    docker compose -p miloco-lab -f "$RELEASES_DIR/$sha/compose.yaml" logs --tail 80 --no-color 2>&1 | sanitize_logs >&2 || true
}

restore_previous() {
    local host="$1" previous_sha="$2"
    [[ -n "$previous_sha" ]] || return 1
    validate_sha "$previous_sha"
    verify_release "$previous_sha"
    docker image inspect "miloco-lab:$previous_sha" >/dev/null 2>&1 || return 1
    compose_up "$host" "$previous_sha"
    wait_for_health "$previous_sha"
}

retain_rollback_history() {
    local current_sha="$1"
    local historical_kept=0 protected_previous="" record release candidate
    if [[ -f "$PREVIOUS_FILE" && ! -L "$PREVIOUS_FILE" ]]; then
        protected_previous="$(<"$PREVIOUS_FILE")"
        [[ "$protected_previous" =~ ^[0-9a-f]{40}$ ]] || protected_previous=""
    fi
    while IFS= read -r record; do
        release="${record#* }"
        candidate="${release##*/}"
        [[ "$candidate" =~ ^[0-9a-f]{40}$ ]] || continue
        [[ "$release" == "$RELEASES_DIR/$candidate" && -d "$release" && ! -L "$release" ]] || continue
        [[ "$candidate" != "$current_sha" ]] || continue
        if [[ -n "$protected_previous" && "$candidate" == "$protected_previous" ]]; then
            historical_kept=$((historical_kept + 1))
            continue
        fi
        if (( historical_kept < 2 )); then
            historical_kept=$((historical_kept + 1))
            continue
        fi
        rm -rf -- "$release"
        docker image rm "miloco-lab:$candidate" "miloco-lab-acceptance:$candidate" >/dev/null 2>&1 || true
    done < <(find "$RELEASES_DIR" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' | sort -rn)
}

activate_release() {
    local host="$1" sha="$2"
    validate_host "$host"
    verify_release "$sha"
    docker image inspect "miloco-lab:$sha" >/dev/null 2>&1 || die 4 "release image is not built"
    [[ ! -L "$DEPLOY_STATE_DIR" ]] || die 4 "deployment state path must not be a symlink"
    install -d -o root -g root -m 0755 "$DEPLOY_STATE_DIR"

    local previous_sha=""
    if [[ -f "$CURRENT_FILE" && ! -L "$CURRENT_FILE" ]]; then
        previous_sha="$(<"$CURRENT_FILE")"
        validate_sha "$previous_sha"
        atomic_write "$PREVIOUS_FILE" "$previous_sha"
    fi

    if ! compose_up "$host" "$sha" || ! wait_for_health "$sha"; then
        collect_failure_evidence "$sha"
        restore_previous "$host" "$previous_sha" || true
        return 1
    fi
    atomic_write "$CURRENT_FILE" "$sha"
    retain_rollback_history "$sha"
}

build_and_activate() {
    local host="$1" sha="$2" release="$RELEASES_DIR/$sha"
    require_root
    validate_host "$host"
    verify_release "$sha"
    preflight_release "$host"
    docker build --platform linux/amd64 --target runtime -t "miloco-lab:$sha" "$release"
    docker build --platform linux/amd64 --target acceptance -t "miloco-lab-acceptance:$sha" "$release"
    docker run --rm --network none "miloco-lab-acceptance:$sha"
    [[ ! -L "$STATE_DIR" ]] || die 4 "persistent state path must not be a symlink"
    install -d -o 10001 -g 10001 -m 0700 "$STATE_DIR"
    activate_release "$host" "$sha"
}

verify_running() {
    local host="$1"
    require_root
    validate_host "$host"
    [[ -f "$CURRENT_FILE" && ! -L "$CURRENT_FILE" ]] || die 4 "not deployed"
    local sha="$(<"$CURRENT_FILE")"
    validate_sha "$sha"
    verify_release "$sha"
    docker image inspect "miloco-lab:$sha" >/dev/null 2>&1 || die 4 "current image is missing"
    wait_for_health "$sha" || die 5 "current release is unhealthy"
    printf 'verified host=%s sha=%s\n' "$host" "$sha"
}

status_release() {
    local host="$1"
    require_root
    validate_host "$host"
    if [[ ! -f "$CURRENT_FILE" || -L "$CURRENT_FILE" ]]; then
        printf 'not_deployed host=%s\n' "$host"
        return 0
    fi
    local sha="$(<"$CURRENT_FILE")"
    validate_sha "$sha"
    local container_id image health
    container_id="$(docker compose -p miloco-lab -f "$RELEASES_DIR/$sha/compose.yaml" ps -q miloco 2>/dev/null || true)"
    image="not_running"
    health="not_running"
    if [[ -n "$container_id" ]]; then
        image="$(docker inspect --format '{{.Config.Image}}' "$container_id" 2>/dev/null || true)"
        health="$(docker inspect --format '{{.State.Health.Status}}' "$container_id" 2>/dev/null || true)"
    fi
    printf 'host=%s current=%s image=%s health=%s\n' "$host" "$sha" "$image" "$health"
}

rollback_release() {
    local host="$1" sha="$2"
    require_root
    validate_host "$host"
    validate_sha "$sha"
    verify_release "$sha"
    docker image inspect "miloco-lab:$sha" >/dev/null 2>&1 || die 4 "rollback image is not built"
    activate_release "$host" "$sha"
}

main() {
    [[ "$#" -ge 2 ]] || die 2 "operation and host are required"
    local operation="$1" host="$2"
    shift 2
    case "$operation" in
        preflight)
            [[ "$#" -eq 0 ]] || die 2 "unexpected preflight argument"
            preflight_release "$host"
            ;;
        activate)
            [[ "$#" -eq 1 ]] || die 2 "activate requires one SHA"
            build_and_activate "$host" "$1"
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
