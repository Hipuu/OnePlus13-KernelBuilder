#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: validate-runner-storage.sh --debug-dir PATH --summary-title TEXT
EOF
}

debug_dir=
summary_title=
while (($#)); do
  case "$1" in
    --debug-dir)
      (($# >= 2)) || { usage; exit 2; }
      debug_dir=$2
      shift 2
      ;;
    --summary-title)
      (($# >= 2)) || { usage; exit 2; }
      summary_title=$2
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

if [[ -z "$debug_dir" || -z "$summary_title" ]]; then
  usage
  exit 2
fi
if [[ -z "${GITHUB_WORKSPACE:-}" || -z "${RUNNER_TEMP:-}" || \
      -z "${GITHUB_STEP_SUMMARY:-}" ]]; then
  echo "required GitHub Actions runner paths are unavailable" >&2
  exit 2
fi
if [[ ! -d "$GITHUB_WORKSPACE" || -L "$GITHUB_WORKSPACE" ]]; then
  echo "GitHub workspace is unavailable or is a symlink" >&2
  exit 2
fi

workspace_root=$(realpath -e -- "$GITHUB_WORKSPACE")
if [[ "$workspace_root" != "$GITHUB_WORKSPACE" ]]; then
  echo "GitHub workspace resolved unexpectedly: $workspace_root" >&2
  exit 2
fi
if ! mountpoint -q -- "$workspace_root"; then
  echo "GitHub workspace is not the pooled build mount" >&2
  exit 2
fi

out_root="$workspace_root/out"
if [[ -L "$out_root" ]]; then
  echo "workspace out path must not be a symlink" >&2
  exit 2
fi
mkdir -p -- "$out_root"
resolved_out=$(realpath -e -- "$out_root")
if [[ "$resolved_out" != "$workspace_root/out" ]]; then
  echo "workspace out path resolved outside the pooled workspace" >&2
  exit 2
fi

if [[ -L "$debug_dir" ]]; then
  echo "debug path must not be a symlink" >&2
  exit 2
fi
mkdir -p -- "$debug_dir"
debug_root=$(realpath -e -- "$debug_dir")
if [[ "$debug_root" != "$workspace_root/out/debug" ]]; then
  echo "debug path resolved outside the expected workspace path: $debug_root" >&2
  exit 2
fi

layout_log="$debug_root/disk-layout.txt"
: > "$layout_log"
fail_layout() {
  printf 'error=%s\n' "$1" >> "$layout_log"
  echo "$1" >&2
  exit 2
}

preparation_dir="$RUNNER_TEMP/op13-pre-lvm"
if [[ ! -d "$preparation_dir" || -L "$preparation_dir" ]]; then
  fail_layout "pre-LVM evidence directory is unavailable or is a symlink"
fi
preparation_root=$(realpath -e -- "$preparation_dir")
expected_preparation_root="$(realpath -e -- "$RUNNER_TEMP")/op13-pre-lvm"
if [[ "$preparation_root" != "$expected_preparation_root" ]]; then
  fail_layout "pre-LVM evidence directory resolved unexpectedly"
fi
properties="$preparation_root/preparation.properties"
if [[ ! -f "$properties" || -L "$properties" ]]; then
  fail_layout "pre-LVM storage properties are unavailable or are a symlink"
fi
property_value() {
  local key=$1
  awk -F= -v key="$key" '$1 == key { print substr($0, length(key) + 2) }' "$properties"
}
storage_mode=$(property_value storage_mode)
root_reserve_mb=$(property_value root_reserve_mb)
temp_reserve_mb=$(property_value temp_reserve_mb)
case "$storage_mode" in
  dual)
    [[ "$root_reserve_mb" == 8448 && "$temp_reserve_mb" == 1024 ]] ||
      fail_layout "dual-device reserve selection changed"
    ;;
  shared)
    [[ "$root_reserve_mb" == 9472 && "$temp_reserve_mb" == 8448 ]] ||
      fail_layout "shared-device reserve selection changed"
    ;;
  *)
    fail_layout "unsupported hosted-runner storage mode: $storage_mode"
    ;;
esac
cp -a -- "$preparation_root/." "$debug_root/"

if [[ ! -d /mnt || -L /mnt || "$(realpath -e -- /mnt)" != /mnt ]]; then
  fail_layout "/mnt is unavailable, is a symlink, or resolves unexpectedly"
fi
for backing_file in /pv.img /mnt/tmp-pv.img; do
  if [[ ! -f "$backing_file" || -L "$backing_file" || \
        "$(realpath -e -- "$backing_file")" != "$backing_file" ]]; then
    fail_layout "LVM backing file is missing, is a symlink, or resolved unexpectedly: $backing_file"
  fi
done

root_device=$(stat -c %d -- /)
temp_device=$(stat -c %d -- /mnt)
workspace_device=$(stat -c %d -- "$workspace_root")
out_device=$(stat -c %d -- "$out_root")
root_backing_device=$(stat -c %d -- /pv.img)
temp_backing_device=$(stat -c %d -- /mnt/tmp-pv.img)
if [[ "$workspace_device" == "$root_device" || "$workspace_device" == "$temp_device" || \
      "$workspace_device" != "$out_device" ]]; then
  fail_layout "workspace/out is not isolated on the pooled filesystem"
fi
case "$storage_mode" in
  dual)
    if [[ "$root_device" == "$temp_device" ]]; then
      fail_layout "dual-device mode does not have a separate /mnt filesystem"
    fi
    if [[ "$root_backing_device" != "$root_device" || \
          "$temp_backing_device" != "$temp_device" ]]; then
      fail_layout "dual-device backing files are not on their expected filesystems"
    fi
    ;;
  shared)
    if [[ "$root_device" != "$temp_device" ]]; then
      fail_layout "shared-device mode unexpectedly has a separate /mnt filesystem"
    fi
    if [[ "$root_backing_device" != "$root_device" || \
          "$temp_backing_device" != "$root_device" ]]; then
      fail_layout "shared-device backing files are not both on the root filesystem"
    fi
    ;;
esac

mount_target=$(findmnt -n -o TARGET --target "$out_root")
mount_source=$(findmnt -n -o SOURCE --target "$out_root")
mount_type=$(findmnt -n -o FSTYPE --target "$out_root")
mount_options=$(findmnt -n -o OPTIONS --target "$out_root")
if [[ "$mount_target" != "$workspace_root" || "$mount_type" != ext4 ]]; then
  fail_layout "workspace out path is not on the expected ext4 workspace mount"
fi
case ",$mount_options," in
  *,rw,*) ;;
  *) fail_layout "pooled workspace mount is not writable" ;;
esac
if [[ ! -b /dev/mapper/buildvg-buildlv ]]; then
  fail_layout "pooled build logical volume is missing"
fi
expected_build_device=$(readlink -f -- /dev/mapper/buildvg-buildlv)
actual_build_device=$(readlink -f -- "$mount_source")
if [[ -z "$expected_build_device" || "$actual_build_device" != "$expected_build_device" ]]; then
  fail_layout "workspace mount is not backed by buildvg/buildlv"
fi

mapfile -t root_loops < <(
  sudo losetup --associated /pv.img --noheadings --output NAME | awk 'NF { print $1 }'
)
mapfile -t temp_loops < <(
  sudo losetup --associated /mnt/tmp-pv.img --noheadings --output NAME | awk 'NF { print $1 }'
)
if ((${#root_loops[@]} != 1 || ${#temp_loops[@]} != 1)); then
  fail_layout "each LVM backing file must have exactly one loop device"
fi
if [[ "${root_loops[0]}" == "${temp_loops[0]}" ]]; then
  fail_layout "root and temporary LVM backing files share a loop device"
fi
mapfile -t build_pvs < <(
  sudo pvs --noheadings --options pv_name,vg_name \
    | awk '$2 == "buildvg" { print $1 }' \
    | LC_ALL=C sort
)
if ((${#build_pvs[@]} != 2)); then
  fail_layout "buildvg must contain exactly two physical volumes"
fi
expected_pvs=$(printf '%s\n' "${root_loops[0]}" "${temp_loops[0]}" | LC_ALL=C sort)
actual_pvs=$(printf '%s\n' "${build_pvs[@]}")
if [[ "$actual_pvs" != "$expected_pvs" ]]; then
  fail_layout "buildvg physical volumes do not match the root and /mnt loop devices"
fi

if [[ ! -b /dev/mapper/buildvg-swap ]]; then
  fail_layout "pooled swap logical volume is missing"
fi
expected_swap_device=$(readlink -f -- /dev/mapper/buildvg-swap)
swap_active=false
while IFS= read -r active_swap; do
  if [[ "$(readlink -f -- "$active_swap")" == "$expected_swap_device" ]]; then
    swap_active=true
    break
  fi
done < <(sudo swapon --show=NAME --noheadings)
if [[ "$swap_active" != true ]]; then
  fail_layout "pooled swap logical volume is not active"
fi
if ! swap_size_bytes=$(sudo blockdev --getsize64 "$expected_swap_device" 2>> "$layout_log"); then
  fail_layout "pooled swap logical volume size is unreadable"
fi
expected_swap_size_bytes=$((8 * 1024 * 1024 * 1024))
if [[ ! "$swap_size_bytes" =~ ^[0-9]+$ ]] ||
   ((swap_size_bytes != expected_swap_size_bytes)); then
  fail_layout "pooled swap logical volume is not exactly 8 GiB"
fi

probe="$out_root/.op13-disk-write-probe"
if ! (umask 077 && printf 'writable\n' > "$probe" && test -s "$probe"); then
  fail_layout "pooled workspace failed its write probe"
fi
rm -f -- "$probe"

root_available=$(df --output=avail -B1 -- / | tail -n 1 | tr -d ' ')
out_available=$(df --output=avail -B1 -- "$out_root" | tail -n 1 | tr -d ' ')
if [[ ! "$root_available" =~ ^[0-9]+$ || ! "$out_available" =~ ^[0-9]+$ ]]; then
  fail_layout "filesystem availability is not numeric"
fi
minimum_root_available=$((8 * 1024 * 1024 * 1024))
minimum_available=$((100 * 1024 * 1024 * 1024))
if ((root_available < minimum_root_available)); then
  fail_layout "less than 8 GiB remains reserved on the root filesystem"
fi
if ((out_available < minimum_available)); then
  fail_layout "less than 100 GiB is available on the pooled build filesystem"
fi

required_commands=(
  bash python3 jq gh git curl make patch depmod zip unzip sha256sum tar xz zstd
  blockdev findmnt mountpoint losetup pvs lvs swapon stat
)
for required_command in "${required_commands[@]}"; do
  if ! command -v "$required_command" >> "$layout_log"; then
    fail_layout "required runner command is unavailable: $required_command"
  fi
done

{
  echo "workspace_root=$workspace_root"
  echo "mount_target=$mount_target"
  echo "mount_source=$mount_source"
  echo "mount_type=$mount_type"
  echo "mount_options=$mount_options"
  echo "storage_mode=$storage_mode"
  echo "root_reserve_mb=$root_reserve_mb"
  echo "temp_reserve_mb=$temp_reserve_mb"
  echo "root_device=$root_device"
  echo "temp_device=$temp_device"
  echo "workspace_device=$workspace_device"
  echo "root_loop_device=${root_loops[0]}"
  echo "temp_loop_device=${temp_loops[0]}"
  echo "swap_size_bytes=$swap_size_bytes"
  echo "root_available_bytes=$root_available"
  echo "workspace_available_bytes=$out_available"
  echo '--- findmnt ---'
  findmnt --target "$out_root"
  echo '--- physical volumes ---'
  sudo pvs
  echo '--- logical volumes ---'
  sudo lvs
  echo '--- loop devices ---'
  sudo losetup --list
  echo '--- swap ---'
  sudo swapon --show
} >> "$layout_log"
df -h / /mnt "$out_root" > "$debug_root/disk-after-lvm.txt"
{
  echo "## $summary_title"
  echo
  echo "- Storage topology: ${storage_mode}"
  echo "- Root reserve available: ${root_available} bytes"
  echo "- Pooled workspace available: ${out_available} bytes"
  echo "- Pooled swap capacity: ${swap_size_bytes} bytes"
  echo "- Workspace mount source: ${mount_source}"
} >> "$GITHUB_STEP_SUMMARY"
