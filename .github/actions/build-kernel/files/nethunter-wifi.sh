#!/system/bin/sh
# nethunter-wifi.sh -- load external Wi-Fi drivers from this module pack.
#
# Why these drivers are modules and not built into the Image
# ----------------------------------------------------------
# ath9k_htc (the AR9271 driver) is "depends on USB && MAC80211" in Kconfig, and
# Kconfig refuses to let a built-in driver depend on a modular provider. Setting
# CONFIG_ATH9K_HTC=y therefore drags CONFIG_MAC80211=y, CONFIG_CFG80211=y and
# CONFIG_RFKILL=y along with it.
#
# That is not survivable on this device. OnePlus builds its own cfg80211 in a
# separate tree, and its symbol CRCs differ from the GKI common tree's -- the
# bundled mac80211 already refuses to load against the platform cfg80211 with
# "disagrees about version of symbol wiphy_new_nm". With cfg80211 compiled into
# the Image, /vendor/lib/modules/cfg80211.ko can no longer load at all, so
# qca_cld3_peach_v2 (the internal Wi-Fi driver) fails its CRC check on *every*
# boot. Built-in ath9k would mean permanently no internal Wi-Fi.
#
# Keeping the drivers modular keeps internal Wi-Fi working until you run this
# script, and a reboot restores it. This script collapses the eight-step manual
# insmod dance into one command and resolves the dependency order itself, from
# the modules.dep shipped alongside the .ko files.
#
# Usage, run as root from the directory this pack was extracted to:
#
#   ./nethunter-wifi.sh load [driver ...]   load drivers (default: ath9k_htc)
#   ./nethunter-wifi.sh restore             unload them, restore internal Wi-Fi
#   ./nethunter-wifi.sh status              show what is currently resident
#   ./nethunter-wifi.sh list                list every driver in this pack
#   ./nethunter-wifi.sh install [driver ...]  autoload at every boot (KernelSU)
#   ./nethunter-wifi.sh uninstall           undo install
#
# Modules are built with CONFIG_MODVERSIONS=y, so this pack only works on the
# exact kernel build it shipped with.

DIR=$(cd "$(dirname "$0")" && pwd)
DEP="$DIR/modules.dep"
TMP="${TMPDIR:-/data/local/tmp}"
ERR="$TMP/.nethunter-wifi.err"
STATE="$TMP/.nethunter-wifi.loaded"
SERVICE_DIR=/data/adb/service.d
INSTALL_DIR=/data/adb/nethunter-wifi

# Modules the platform stack owns. Displacing these is what costs internal
# Wi-Fi, so it happens only when a requested driver actually needs mac80211.
PLATFORM_MODULES="qca_cld3_peach_v2 mac80211 cfg80211"
PLATFORM_DIR=/vendor/lib/modules

# Where the Nethunter firmware ZIP puts its blobs. ath9k_htc asks for
# ath9k_htc/htc_9271-1.4.0.fw; the firmware loader only searches /lib/firmware
# by default, which does not exist on Android, so point it at the real location.
FIRMWARE_PARAM=/sys/module/firmware_class/parameters/path
FIRMWARE_CANDIDATES="/vendor/firmware /system/etc/firmware /system/vendor/firmware /odm/firmware /lib/firmware"
FIRMWARE_PROBE=ath9k_htc/htc_9271-1.4.0.fw

DEFAULT_DRIVERS=ath9k_htc

die() { echo "error: $*" >&2; exit 1; }

need_root() {
  [ "$(id -u)" = 0 ] || die "must run as root (su -c '$0 $*')"
}

# /proc/modules spells names with underscores; filenames may use dashes.
procname() { echo "$1" | tr '-' '_'; }

resident() { grep -q "^$(procname "$1") " /proc/modules 2>/dev/null; }

# Direct dependencies of $1, from the flattened modules.dep shipped in the ZIP.
# grep/cut rather than awk: Android's toybox has no awk. Module names are
# [a-z0-9_-] only, so none of them are regex metacharacters.
dep_of() { [ -f "$DEP" ] && grep -m1 "^$1:" "$DEP" 2>/dev/null | cut -d: -f2-; }

have_module() { [ -f "$DIR/$1.ko" ]; }

in_list() {
  _needle=$1
  shift
  case " $* " in *" $_needle "*) return 0 ;; esac
  return 1
}

# Full dependency closure of the given modules, including themselves.
# Iterative worklist, not recursion: POSIX sh has no local variables, so a
# recursive walk clobbers its own loop counters.
closure_of() {
  _pending=$*
  _seen=
  while [ -n "$_pending" ]; do
    _next=
    for _m in $_pending; do
      in_list "$_m" $_seen && continue
      _seen="$_seen $_m"
      for _d in $(dep_of "$_m"); do
        in_list "$_d" $_seen && continue
        _next="$_next $_d"
      done
    done
    _pending=$_next
  done
  echo $_seen
}

# Point the firmware loader at whichever directory actually holds the blobs.
set_firmware_path() {
  [ -w "$FIRMWARE_PARAM" ] || return 0
  for d in $FIRMWARE_CANDIDATES; do
    if [ -f "$d/$FIRMWARE_PROBE" ]; then
      echo "$d" > "$FIRMWARE_PARAM" 2>/dev/null && echo "  firmware path: $d"
      return 0
    fi
  done
  echo "  warning: $FIRMWARE_PROBE not found in: $FIRMWARE_CANDIDATES"
  echo "           extract the Nethunter-Wireless-Firmware ZIP first, or"
  echo "           ath9k_htc will attach and then fail to fetch firmware."
}

# Load a module whose dependencies are already resident.
insmod_one() {
  if insmod "$DIR/$1.ko" 2>"$ERR"; then
    echo "  loaded  $1"
    echo "$1" >> "$STATE"
    return 0
  fi
  case "$(cat "$ERR" 2>/dev/null)" in
    *"File exists"*)
      # Already provided by the platform under the same name.
      echo "  present $1"
      return 0
      ;;
  esac
  return 1
}

# Insmod the closure of $@ in dependency order. Repeated passes over the set,
# loading whatever has all its dependencies satisfied, until nothing new loads.
# A fixed point beats a topological sort here: it needs no recursion and copes
# with a modules.dep whose ordering cannot be trusted.
load_closure() {
  _want=$(closure_of "$@")
  _rc=0
  _pass=0
  while [ "$_pass" -lt 32 ]; do
    _progress=0
    _left=
    for _m in $_want; do
      resident "$_m" && continue
      if ! have_module "$_m"; then
        echo "  MISSING $_m.ko (not in this pack)"
        _rc=1
        continue
      fi
      _ready=1
      for _d in $(dep_of "$_m"); do
        resident "$_d" || _ready=0
      done
      if [ "$_ready" = 1 ]; then
        if insmod_one "$_m"; then
          _progress=1
        else
          echo "  FAILED  $_m: $(cat "$ERR" 2>/dev/null)"
          _rc=1
        fi
      else
        _left="$_left $_m"
      fi
    done
    _want=$_left
    [ -z "$_want" ] && break
    [ "$_progress" = 0 ] && break
    _pass=$((_pass + 1))
  done
  for _m in $_want; do
    echo "  STUCK   $_m (dependencies never became available)"
    _rc=1
  done
  return "$_rc"
}

# True if $1 needs mac80211, i.e. loading it displaces the platform stack.
needs_mac80211() {
  in_list mac80211 $(closure_of "$1")
}

wifi_cmd() { cmd wifi "$@" >/dev/null 2>&1; }

unload_platform_stack() {
  echo "=== displacing platform Wi-Fi stack ==="
  echo "  (internal Wi-Fi stops working until you reboot or run: $0 restore)"
  wifi_cmd set-wifi-enabled disabled
  sleep 2
  for m in $PLATFORM_MODULES; do
    if resident "$m"; then
      if rmmod "$m" 2>"$ERR"; then
        echo "  unloaded $m"
      else
        echo "  could not unload $m: $(cat "$ERR" 2>/dev/null)"
      fi
    fi
  done
}

cmd_load() {
  need_root
  drivers=${*:-$DEFAULT_DRIVERS}
  [ -f "$DEP" ] || die "modules.dep missing next to $0"

  for d in $drivers; do
    have_module "$d" || die "no $d.ko in this pack (try: $0 list)"
  done

  swap=0
  for d in $drivers; do
    needs_mac80211 "$d" && swap=1
  done
  [ "$swap" = 1 ] && unload_platform_stack

  echo "=== firmware ==="
  set_firmware_path

  echo "=== loading: $drivers ==="
  : > "$STATE"
  rc=0
  load_closure $drivers || rc=1

  echo
  if [ "$rc" = 0 ]; then
    echo "Loaded. Plug the adapter in; check with: $0 status"
  else
    echo "One or more modules failed. Kernel log:"
    dmesg | grep -iE "unknown symbol|disagrees about version" | tail -5
  fi
  return "$rc"
}

cmd_restore() {
  need_root
  echo "=== unloading modules this script loaded ==="
  if [ -s "$STATE" ]; then
    # Fixed point rather than reverse order: rmmod refuses a module while
    # anything still references it, so repeat until a pass frees nothing.
    pass=0
    while [ "$pass" -lt 32 ]; do
      progress=0
      for n in $(cat "$STATE"); do
        resident "$n" || continue
        rmmod "$(procname "$n")" 2>/dev/null && progress=1
      done
      [ "$progress" = 0 ] && break
      pass=$((pass + 1))
    done
    for n in $(cat "$STATE"); do
      resident "$n" && echo "  still loaded: $n"
    done
    : > "$STATE"
  else
    echo "  nothing recorded as loaded"
  fi

  echo "=== restoring platform Wi-Fi stack ==="
  for m in cfg80211 mac80211 qca_cld3_peach_v2; do
    if resident "$m"; then
      echo "  $m already resident"
    elif [ -f "$PLATFORM_DIR/$m.ko" ]; then
      insmod "$PLATFORM_DIR/$m.ko" 2>"$ERR" \
        && echo "  restored $m" \
        || echo "  $m: $(cat "$ERR" 2>/dev/null)"
    fi
  done
  wifi_cmd set-wifi-enabled enabled

  echo
  echo "Android's WifiService may stay latched in a failed state."
  echo "If internal Wi-Fi does not come back, reboot."
}

cmd_status() {
  echo "=== pack modules resident ==="
  cnt=0
  for f in "$DIR"/*.ko; do
    [ -f "$f" ] || continue
    n=$(basename "$f" .ko)
    if resident "$n"; then
      echo "  $n"
      cnt=$((cnt + 1))
    fi
  done
  echo "  ($cnt resident)"
  echo
  echo "=== wireless interfaces ==="
  ls /sys/class/ieee80211 2>/dev/null || echo "  none"
  echo
  echo "=== firmware search path ==="
  cat "$FIRMWARE_PARAM" 2>/dev/null || echo "  (default)"
}

cmd_list() {
  echo "Drivers in this pack:"
  for f in "$DIR"/*.ko; do
    [ -f "$f" ] || continue
    echo "  $(basename "$f" .ko)"
  done
}

cmd_install() {
  need_root
  drivers=${*:-$DEFAULT_DRIVERS}
  [ -d "$SERVICE_DIR" ] || die "$SERVICE_DIR missing (KernelSU/Magisk not installed?)"

  echo "WARNING: this loads the drivers at every boot, which means the"
  echo "         internal Wi-Fi will not work on any boot. Undo with:"
  echo "         $0 uninstall"
  echo

  mkdir -p "$INSTALL_DIR" || die "cannot create $INSTALL_DIR"
  cp -f "$DIR"/*.ko "$INSTALL_DIR"/ || die "cannot copy modules"
  cp -f "$DEP" "$INSTALL_DIR"/ || die "cannot copy modules.dep"
  cp -f "$0" "$INSTALL_DIR"/nethunter-wifi.sh || die "cannot copy script"
  chmod 755 "$INSTALL_DIR"/nethunter-wifi.sh

  {
    echo '#!/system/bin/sh'
    echo '# Generated by nethunter-wifi.sh install. Remove with: uninstall'
    echo '# Wait for the Wi-Fi framework to finish coming up before displacing it.'
    echo 'until [ "$(getprop sys.boot_completed)" = 1 ]; do sleep 2; done'
    echo 'sleep 10'
    echo "$INSTALL_DIR/nethunter-wifi.sh load $drivers >> /data/local/tmp/nethunter-wifi.log 2>&1"
  } > "$SERVICE_DIR/nethunter-wifi.sh"
  chmod 755 "$SERVICE_DIR/nethunter-wifi.sh"

  echo "Installed to $INSTALL_DIR"
  echo "Boot service: $SERVICE_DIR/nethunter-wifi.sh (drivers: $drivers)"
  echo "Boot log:     /data/local/tmp/nethunter-wifi.log"
}

cmd_uninstall() {
  need_root
  rm -f "$SERVICE_DIR/nethunter-wifi.sh"
  rm -rf "$INSTALL_DIR"
  echo "Removed boot service and $INSTALL_DIR."
  echo "Reboot to get the internal Wi-Fi back."
}

action=${1:-load}
[ $# -gt 0 ] && shift
case "$action" in
  load)      cmd_load "$@" ;;
  restore)   cmd_restore ;;
  status)    cmd_status ;;
  list)      cmd_list ;;
  install)   cmd_install "$@" ;;
  uninstall) cmd_uninstall ;;
  -h|--help|help)
    sed -n '23,32p' "$0" | sed -e 's/^#  *//' -e 's/^#$//'
    ;;
  *)
    die "unknown command '$action' (try: $0 --help)"
    ;;
esac
