#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: run-hosted-source-sync.sh --base BASE --output PATH --debug-dir PATH
EOF
}

base=
output=
debug_dir=
while (($#)); do
  case "$1" in
    --base)
      (($# >= 2)) || { usage; exit 2; }
      base=$2
      shift 2
      ;;
    --output)
      (($# >= 2)) || { usage; exit 2; }
      output=$2
      shift 2
      ;;
    --debug-dir)
      (($# >= 2)) || { usage; exit 2; }
      debug_dir=$2
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

case "$base" in
  oos15-cn|oos15-global|oos16) ;;
  *)
    echo "unsupported OnePlus base: $base" >&2
    exit 2
    ;;
esac
if [[ -z "$output" || -z "$debug_dir" || -z "${GITHUB_WORKSPACE:-}" ]]; then
  usage
  exit 2
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
repo_root=$(realpath -e -- "$script_dir/..")
workspace_root=$(realpath -e -- "$GITHUB_WORKSPACE")
if [[ "$repo_root" != "$workspace_root" ]]; then
  echo "hosted source sync must run from the checked-out GitHub workspace" >&2
  exit 2
fi
for path in "$output" "$debug_dir"; do
  if [[ ! -d "$path" || -L "$path" ]]; then
    echo "hosted source-sync path is missing or is a symlink: $path" >&2
    exit 2
  fi
done
output_root=$(realpath -e -- "$output")
debug_root=$(realpath -e -- "$debug_dir")
if [[ "$output_root" != "$workspace_root/out/source" ||
      "$debug_root" != "$workspace_root/out/debug" ]]; then
  echo "hosted source-sync paths resolved outside the canonical workspace layout" >&2
  exit 2
fi

sync_log="$debug_root/source-sync.log"
telemetry_log="$debug_root/source-sync-telemetry.log"
for log in "$sync_log" "$telemetry_log"; do
  if [[ -e "$log" || -L "$log" ]]; then
    echo "hosted source-sync log already exists: $log" >&2
    exit 2
  fi
done

checkout_jobs=2
telemetry_interval_seconds=60
sync_started_epoch=$(date -u +%s)

record_snapshot() {
  local snapshot_epoch snapshot_timestamp elapsed_seconds load_one workspace_available_bytes
  snapshot_epoch=$(date -u +%s)
  snapshot_timestamp=$(date -u +'%Y-%m-%dT%H:%M:%SZ')
  elapsed_seconds=$((snapshot_epoch - sync_started_epoch))
  load_one=$(cut -d ' ' -f1 /proc/loadavg)
  workspace_available_bytes=$(df --output=avail -B1 "$workspace_root" |
    tail -n 1 | tr -d ' ')
  {
    echo "=== $snapshot_timestamp ==="
    echo "elapsed_seconds=$elapsed_seconds"
    echo "checkout_jobs=$checkout_jobs"
    echo "telemetry_interval_seconds=$telemetry_interval_seconds"
    echo '--- load ---'
    cat /proc/loadavg
    echo '--- memory ---'
    free -h
    echo '--- filesystems ---'
    df -h / "$workspace_root"
    echo '--- top resident processes ---'
    ps -eo pid,ppid,state,pcpu,pmem,rss,vsz,etimes,comm --sort=-rss |
      sed -n '1,21p'
    echo
  } >> "$telemetry_log"
  printf '[source-sync heartbeat] utc=%s elapsed_seconds=%s load1=%s workspace_available_bytes=%s\n' \
    "$snapshot_timestamp" "$elapsed_seconds" "$load_one" "$workspace_available_bytes"
}

observer_pid=
stop_observer() {
  if [[ -n "$observer_pid" ]] && kill -0 "$observer_pid" 2>/dev/null; then
    kill "$observer_pid" 2>/dev/null || true
    wait "$observer_pid" 2>/dev/null || true
  fi
  observer_pid=
}
handle_signal() {
  stop_observer
  exit 130
}
trap stop_observer EXIT
trap handle_signal INT TERM

record_snapshot
(
  while sleep "$telemetry_interval_seconds"; do
    record_snapshot
  done
) &
observer_pid=$!

set +e
bash "$repo_root/scripts/sync-sources.sh" \
  --base "$base" \
  --output "$output_root" \
  --jobs "$checkout_jobs" \
  2>&1 | tee "$sync_log"
pipeline_status=("${PIPESTATUS[@]}")
set -e

stop_observer
trap - EXIT INT TERM
record_snapshot

sync_status=${pipeline_status[0]:-125}
tee_status=${pipeline_status[1]:-125}
if ((sync_status != 0)); then
  echo "locked source synchronization failed with exit $sync_status" >&2
  exit "$sync_status"
fi
if ((tee_status != 0)); then
  echo "source-sync log capture failed with exit $tee_status" >&2
  exit "$tee_status"
fi
