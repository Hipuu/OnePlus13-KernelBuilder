#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: run-observed-kernel-build.sh --debug-dir PATH -- bash scripts/build-kernel.sh [ARGS...]
EOF
}

debug_dir=
while (($#)); do
  case "$1" in
    --debug-dir)
      (($# >= 2)) || { usage; exit 2; }
      debug_dir=$2
      shift 2
      ;;
    --)
      shift
      break
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown observer argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done
command=("$@")

if [[ -z "$debug_dir" || -z "${GITHUB_WORKSPACE:-}" ||
      ${#command[@]} -lt 2 ]]; then
  usage
  exit 2
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
repo_root=$(realpath -e -- "$script_dir/..")
workspace_root=$(realpath -e -- "$GITHUB_WORKSPACE")
working_root=$(pwd -P)
if [[ "$repo_root" != "$workspace_root" || "$working_root" != "$workspace_root" ]]; then
  echo "observed kernel build must run from the checked-out GitHub workspace" >&2
  exit 2
fi
if [[ ! -d "$debug_dir" || -L "$debug_dir" ]]; then
  echo "kernel-build debug directory is missing or is a symlink: $debug_dir" >&2
  exit 2
fi
debug_root=$(realpath -e -- "$debug_dir")
if [[ "$debug_root" != "$workspace_root/out/debug" ]]; then
  echo "kernel-build debug directory resolved outside out/debug" >&2
  exit 2
fi
if [[ "${command[0]}" != bash ||
      "${command[1]}" != scripts/build-kernel.sh ||
      -L "${command[1]}" ]]; then
  echo "observer accepts only the repository kernel-build command" >&2
  exit 2
fi
build_script=$(realpath -e -- "${command[1]}")
if [[ "$build_script" != "$repo_root/scripts/build-kernel.sh" ]]; then
  echo "kernel-build command resolved outside the checked-out repository" >&2
  exit 2
fi

telemetry_log="$debug_root/kernel-build-telemetry.log"
if [[ -e "$telemetry_log" || -L "$telemetry_log" ]]; then
  echo "kernel-build telemetry log already exists: $telemetry_log" >&2
  exit 2
fi
: > "$telemetry_log"

telemetry_interval_seconds=60
build_started_epoch=$(date -u +%s)

memory_value() {
  local name=$1 value
  value=$(awk -v name="$name" '$1 == name ":" { print $2; exit }' \
    /proc/meminfo 2>/dev/null || true)
  printf '%s\n' "${value:-unavailable}"
}

record_snapshot() {
  local snapshot_epoch snapshot_timestamp elapsed_seconds load_one
  local mem_available_kib swap_total_kib swap_free_kib swap_used_kib
  local workspace_available_bytes heartbeat
  snapshot_epoch=$(date -u +%s 2>/dev/null || printf '%s' "$build_started_epoch")
  snapshot_timestamp=$(date -u +'%Y-%m-%dT%H:%M:%SZ' 2>/dev/null ||
    printf '%s' 'unavailable')
  elapsed_seconds=$((snapshot_epoch - build_started_epoch))
  load_one=unavailable
  if [[ -r /proc/loadavg ]]; then
    read -r load_one _ < /proc/loadavg || load_one=unavailable
  fi
  mem_available_kib=$(memory_value MemAvailable)
  swap_total_kib=$(memory_value SwapTotal)
  swap_free_kib=$(memory_value SwapFree)
  swap_used_kib=unavailable
  if [[ "$swap_total_kib" =~ ^[0-9]+$ &&
        "$swap_free_kib" =~ ^[0-9]+$ ]]; then
    swap_used_kib=$((swap_total_kib - swap_free_kib))
  fi
  workspace_available_bytes=$(df --output=avail -B1 "$workspace_root" \
    2>/dev/null | tail -n 1 | tr -d ' ' || true)
  workspace_available_bytes=${workspace_available_bytes:-unavailable}
  {
    echo "=== $snapshot_timestamp ==="
    echo "elapsed_seconds=$elapsed_seconds"
    echo "telemetry_interval_seconds=$telemetry_interval_seconds"
    echo "load1=$load_one"
    echo "mem_available_kib=$mem_available_kib"
    echo "swap_total_kib=$swap_total_kib"
    echo "swap_free_kib=$swap_free_kib"
    echo "swap_used_kib=$swap_used_kib"
    echo "workspace_available_bytes=$workspace_available_bytes"
    echo '--- highest RSS processes (KiB) ---'
    if command -v ps >/dev/null 2>&1; then
      ps -eo pid,ppid,state,pcpu,pmem,rss,vsz,etimes,comm --sort=-rss \
        2>&1 | sed -n '1,21p' || echo 'process snapshot unavailable'
    else
      echo 'process snapshot unavailable'
    fi
    echo
  } >> "$telemetry_log" 2>&1 || true
  heartbeat="[kernel-build heartbeat] utc=$snapshot_timestamp"
  heartbeat+=" elapsed_seconds=$elapsed_seconds load1=$load_one"
  heartbeat+=" mem_available_kib=$mem_available_kib"
  heartbeat+=" swap_used_kib=$swap_used_kib"
  heartbeat+=" workspace_available_bytes=$workspace_available_bytes"
  printf '%s\n' "$heartbeat" || true
  return 0
}

observer_pid=
build_pid=
build_pgid=
stop_observer() {
  if [[ -n "$observer_pid" ]] && kill -0 "$observer_pid" 2>/dev/null; then
    kill "$observer_pid" 2>/dev/null || true
    wait "$observer_pid" 2>/dev/null || true
  fi
  observer_pid=
}
stop_build_group() {
  local signal_name=${1:-TERM} attempt
  if [[ -z "$build_pid" || -z "$build_pgid" ]]; then
    return 0
  fi
  if kill -0 -- "-$build_pgid" 2>/dev/null; then
    kill -s "$signal_name" -- "-$build_pgid" 2>/dev/null || true
  elif kill -0 "$build_pid" 2>/dev/null; then
    # Cover the tiny interval before setsid establishes the new process group.
    kill -s "$signal_name" "$build_pid" 2>/dev/null || true
  fi
  attempt=0
  while ((attempt < 50)); do
    if ! kill -0 -- "-$build_pgid" 2>/dev/null; then
      break
    fi
    sleep 0.1
    ((attempt += 1))
  done
  if kill -0 -- "-$build_pgid" 2>/dev/null; then
    kill -s KILL -- "-$build_pgid" 2>/dev/null || true
  fi
  wait "$build_pid" 2>/dev/null || true
  build_pid=
  build_pgid=
}
# Invoked indirectly by the EXIT trap below.
# shellcheck disable=SC2329
cleanup() {
  stop_observer
  stop_build_group TERM
}
handle_signal() {
  local signal_name=$1 status=$2
  trap - INT TERM
  stop_observer
  stop_build_group "$signal_name"
  exit "$status"
}
trap cleanup EXIT
trap 'handle_signal INT 130' INT
trap 'handle_signal TERM 143' TERM

record_snapshot
(
  sleep_pid=
  # Invoked indirectly by the subshell's signal traps below.
  # shellcheck disable=SC2329
  stop_observer_sleep() {
    if [[ -n "$sleep_pid" ]] && kill -0 "$sleep_pid" 2>/dev/null; then
      kill "$sleep_pid" 2>/dev/null || true
      wait "$sleep_pid" 2>/dev/null || true
    fi
    exit 0
  }
  trap stop_observer_sleep INT TERM
  while true; do
    sleep "$telemetry_interval_seconds" &
    sleep_pid=$!
    wait "$sleep_pid"
    sleep_pid=
    record_snapshot
  done
) &
observer_pid=$!

if ! command -v setsid >/dev/null 2>&1; then
  echo "setsid is required to isolate and cancel the kernel build process group" >&2
  exit 2
fi

pending_signal=
pending_status=
# Invoked indirectly during the short signal-handoff trap window below.
# shellcheck disable=SC2329
queue_signal() {
  pending_signal=$1
  pending_status=$2
}
trap 'queue_signal INT 130' INT
trap 'queue_signal TERM 143' TERM
setsid -- "${command[@]}" &
build_pid=$!
build_pgid=$build_pid
trap 'handle_signal INT 130' INT
trap 'handle_signal TERM 143' TERM
if [[ -n "$pending_signal" ]]; then
  handle_signal "$pending_signal" "$pending_status"
fi
set +e
wait "$build_pid"
build_status=$?
set -e
# A failed launcher can exit while descendants in its isolated process group
# are still alive. Reap that entire group before reporting the launcher status.
stop_build_group TERM

stop_observer
trap - EXIT INT TERM
record_snapshot
exit "$build_status"
