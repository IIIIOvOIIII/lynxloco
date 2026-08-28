#!/usr/bin/env sh
set -eu

umask 077

state_dir="${MILOCO_HOME:-/var/lib/miloco}"

fail() {
    printf '%s\n' "$1" >&2
    exit 70
}

[ "$state_dir" = "/var/lib/miloco" ] || fail "state directory must be /var/lib/miloco"
[ -d "$state_dir" ] || fail "state directory is missing"
[ -w "$state_dir" ] || fail "state directory is not writable"

actual_owner="$(stat -c '%u:%g' "$state_dir")"
expected_owner="$(id -u):$(id -g)"
[ "$actual_owner" = "$expected_owner" ] || fail "state directory owner is not the runtime user"

state_mode="$(stat -c '%a' "$state_dir")"
group_world_mode="${state_mode#${state_mode%??}}"
[ "$group_world_mode" = "00" ] || fail "state directory must not be group/world accessible"

exec miloco-backend "$@"
