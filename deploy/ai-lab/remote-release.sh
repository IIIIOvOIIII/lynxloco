#!/usr/bin/env bash

set -euo pipefail

readonly LAB_ROOT="/opt/miloco-lab"
readonly RELEASES_DIR="$LAB_ROOT/releases"
readonly STATE_DIR="$LAB_ROOT/state"
readonly DEPLOY_STATE_DIR="$LAB_ROOT/deploy-state"
readonly CURRENT_FILE="$DEPLOY_STATE_DIR/current"
readonly PREVIOUS_FILE="$DEPLOY_STATE_DIR/previous"
readonly INCOMING_DIR="$LAB_ROOT/incoming"
readonly ARTIFACT_RECORDS_DIR="$DEPLOY_STATE_DIR/artifacts"
readonly ACCEPTED_DIR="$DEPLOY_STATE_DIR/accepted"
readonly TRANSITION_LOCK_FILE="$DEPLOY_STATE_DIR/transition.lock"
readonly HEALTH_TIMEOUT_SECONDS=120
readonly MINIMUM_DISK_KIB=5242880
readonly ROLLBACK_FAILED_EXIT_CODE=70
readonly ZERO_SHA="0000000000000000000000000000000000000000"

transition_armed=0
transition_host=""
transition_candidate=""
transition_previous=""

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
        timeout --signal=TERM --kill-after=2s "${timeout_seconds}s" \
        docker compose "${compose_args[@]}"
}

docker_command() {
    local timeout_seconds="$1"
    shift
    [[ "$timeout_seconds" =~ ^[1-9][0-9]*$ ]] || return 2
    timeout --signal=TERM --kill-after=2s "${timeout_seconds}s" docker "$@"
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
    ) >/dev/null
}

verify_release() {
    local sha="$1"
    validate_sha "$sha"
    local release="$RELEASES_DIR/$sha"
    [[ "$release" == "/opt/miloco-lab/releases/$sha" ]] || die 4 "release path mismatch"
    [[ ! -L "$LAB_ROOT" && ! -L "$RELEASES_DIR" && ! -L "$STATE_DIR" && ! -L "$DEPLOY_STATE_DIR" ]] || die 4 "lab paths must not be symlinks"
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

receive_release() {
    local host="$1" sha="$2" expected_digest="$3"
    require_root
    validate_host "$host"
    validate_sha "$sha"
    [[ "$expected_digest" =~ ^[0-9a-f]{64}$ ]] || die 2 "invalid archive digest"
    [[ ! -L "$LAB_ROOT" && ! -L "$RELEASES_DIR" && ! -L "$INCOMING_DIR" && ! -L "$DEPLOY_STATE_DIR" ]] || die 4 "lab paths must not be symlinks"
    acquire_transition_lock
    install -d -o root -g root -m 0755 "$LAB_ROOT" "$RELEASES_DIR" "$DEPLOY_STATE_DIR" "$ARTIFACT_RECORDS_DIR"
    install -d -o root -g root -m 0700 "$INCOMING_DIR"
    local release="$RELEASES_DIR/$sha"

    local incoming staging published_release="" recorded_digest
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
        recorded_digest="$(artifact_digest_for "$sha")" || die 4 "existing artifact digest record is missing"
        [[ "$recorded_digest" == "$expected_digest" ]] || die 4 "existing release artifact digest mismatch"
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
    atomic_write "$ARTIFACT_RECORDS_DIR/$sha" "$expected_digest"
    published_release=""
    trap - EXIT
    rm -f -- "$incoming"
}

atomic_write() {
    local destination="$1" value="$2"
    local temporary="${destination}.tmp.$$"
    if ! printf '%s\n' "$value" > "$temporary"; then
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
}

acquire_transition_lock() {
    [[ ! -L "$DEPLOY_STATE_DIR" && ! -L "$TRANSITION_LOCK_FILE" ]] || die 4 "deployment lock path must not be a symlink"
    install -d -o root -g root -m 0755 "$DEPLOY_STATE_DIR"
    exec 9> "$TRANSITION_LOCK_FILE"
    flock -n 9 || die 6 "deployment transition is locked"
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
    http_status="$(timeout --signal=TERM --kill-after=1s "${remaining}s" \
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

artifact_digest_for() {
    local sha="$1" record="$ARTIFACT_RECORDS_DIR/$sha" digest
    [[ -f "$record" && ! -L "$record" ]] || return 1
    digest="$(<"$record")"
    [[ "$digest" =~ ^[0-9a-f]{64}$ ]] || return 1
    printf '%s\n' "$digest"
}

mark_acceptance_success() {
    local sha="$1" digest
    digest="$(artifact_digest_for "$sha")" || die 4 "artifact digest record is missing"
    [[ ! -L "$ACCEPTED_DIR" ]] || die 4 "acceptance marker path must not be a symlink"
    install -d -o root -g root -m 0755 "$ACCEPTED_DIR"
    atomic_write "$ACCEPTED_DIR/$sha" "$digest"
}

rollback_capable() {
    local sha="$1" digest marker runtime_image acceptance_image
    validate_sha "$sha"
    (verify_release "$sha") >/dev/null 2>&1 || return 1
    digest="$(artifact_digest_for "$sha")" || return 1
    marker="$ACCEPTED_DIR/$sha"
    [[ -f "$marker" && ! -L "$marker" && "$(<"$marker")" == "$digest" ]] || return 1
    runtime_image="miloco-lab:$sha"
    acceptance_image="miloco-lab-acceptance:$sha"
    docker_command 15 image inspect "$runtime_image" >/dev/null 2>&1 || return 1
    docker_command 15 image inspect "$acceptance_image" >/dev/null 2>&1 || return 1
}

restore_previous() {
    local host="$1" previous_sha="$2"
    [[ -n "$previous_sha" ]] || return 1
    validate_sha "$previous_sha"
    rollback_capable "$previous_sha" || return 1
    compose_up "$host" "$previous_sha" || return 1
    wait_for_health "$host" "$previous_sha" || return 1
    atomic_write "$CURRENT_FILE" "$previous_sha" || return 1
}

stop_candidate() {
    local host="$1" candidate_sha="$2" container_id query_status
    compose_command "$host" "$candidate_sha" 30 stop --timeout 15 miloco || return 1
    if container_id="$(compose_container_id "$host" "$candidate_sha" 10)"; then
        return 1
    else
        query_status="$?"
    fi
    (( query_status == 1 ))
}

transition_exit() {
    local original_exit="$?" recovery_status=1
    trap - EXIT HUP INT TERM
    transition_armed=0
    set +e
    if [[ -n "$transition_previous" ]]; then
        restore_previous "$transition_host" "$transition_previous"
        recovery_status="$?"
    else
        stop_candidate "$transition_host" "$transition_candidate"
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
    transition_armed=1
    trap transition_exit EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
}

commit_transition() {
    transition_armed=0
    trap - EXIT HUP INT TERM
}

retain_rollback_history() {
    local current_sha="$1"
    local historical_kept=0 protected_previous="" record release candidate
    rollback_capable "$current_sha" || die 4 "current release is not rollback capable"
    if [[ -f "$PREVIOUS_FILE" && ! -L "$PREVIOUS_FILE" ]]; then
        protected_previous="$(<"$PREVIOUS_FILE")"
        [[ "$protected_previous" =~ ^[0-9a-f]{40}$ ]] || protected_previous=""
    fi
    if [[ -n "$protected_previous" && "$protected_previous" != "$current_sha" ]]; then
        if rollback_capable "$protected_previous"; then
            historical_kept=1
        else
            remove_release_pair "$protected_previous"
            protected_previous=""
        fi
    fi
    while IFS= read -r record; do
        release="${record#* }"
        candidate="${release##*/}"
        [[ "$candidate" =~ ^[0-9a-f]{40}$ ]] || continue
        [[ "$release" == "$RELEASES_DIR/$candidate" && -d "$release" && ! -L "$release" ]] || continue
        [[ "$candidate" != "$current_sha" ]] || continue
        if [[ -n "$protected_previous" && "$candidate" == "$protected_previous" ]]; then
            continue
        fi
        if ! rollback_capable "$candidate"; then
            remove_release_pair "$candidate"
        elif (( historical_kept < 2 )); then
            historical_kept=$((historical_kept + 1))
        else
            remove_release_pair "$candidate"
        fi
    done < <(find "$RELEASES_DIR" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' | sort -rn)
}

remove_release_pair() {
    local sha="$1" release
    validate_sha "$sha"
    release="$RELEASES_DIR/$sha"
    [[ "$release" == "/opt/miloco-lab/releases/$sha" ]] || die 4 "release cleanup path mismatch"
    [[ ! -L "$release" ]] || die 4 "release cleanup symlink is forbidden"
    [[ ! -e "$release" ]] || rm -rf -- "$release"
    docker_command 30 image rm "miloco-lab:$sha" "miloco-lab-acceptance:$sha" >/dev/null 2>&1 || true
}

activate_release() {
    local host="$1" sha="$2"
    validate_host "$host"
    rollback_capable "$sha" || die 4 "release is not verified and acceptance-approved"
    [[ ! -L "$DEPLOY_STATE_DIR" ]] || die 4 "deployment state path must not be a symlink"
    install -d -o root -g root -m 0755 "$DEPLOY_STATE_DIR"

    local previous_sha=""
    if [[ -f "$CURRENT_FILE" && ! -L "$CURRENT_FILE" ]]; then
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
    retain_rollback_history "$sha"
}

build_and_activate() {
    local host="$1" sha="$2" release="$RELEASES_DIR/$sha"
    require_root
    validate_host "$host"
    acquire_transition_lock
    verify_release "$sha"
    preflight_release "$host"
    docker_command 3600 build --platform linux/amd64 --target runtime -t "miloco-lab:$sha" "$release"
    docker_command 3600 build --platform linux/amd64 --target acceptance -t "miloco-lab-acceptance:$sha" "$release"
    docker_command 900 run --rm --network none "miloco-lab-acceptance:$sha"
    mark_acceptance_success "$sha"
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
    rollback_capable "$sha" || die 4 "current release is not rollback capable"
    wait_for_health "$host" "$sha" || die 5 "current release is unhealthy"
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
    local host="$1" sha="$2"
    require_root
    validate_host "$host"
    acquire_transition_lock
    validate_sha "$sha"
    rollback_capable "$sha" || die 4 "rollback release is not verified and acceptance-approved"
    activate_release "$host" "$sha"
}

main() {
    [[ "$#" -ge 2 ]] || die 2 "operation and host are required"
    local operation="$1" host="$2"
    shift 2
    case "$operation" in
        receive)
            [[ "$#" -eq 2 ]] || die 2 "receive requires SHA and archive digest"
            receive_release "$host" "$1" "$2"
            ;;
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
