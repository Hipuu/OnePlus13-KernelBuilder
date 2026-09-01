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
# Modules are built with CONFIG_MODVERSIONS=y, so this pack only works on the
# exact kernel build it shipped with.
#
# Usage, run as root from the directory this pack was extracted to:
#
#   ./nethunter-wifi.sh                     interactive menu (no arguments)
#   ./nethunter-wifi.sh load [driver ...]   load drivers (default: ath9k_htc)
#   ./nethunter-wifi.sh restore             unload them, restore internal Wi-Fi
#   ./nethunter-wifi.sh status              show what is currently resident
#   ./nethunter-wifi.sh list                list every driver in this pack
#   ./nethunter-wifi.sh monitor [if] [ch]   put the adapter into monitor mode
#   ./nethunter-wifi.sh managed [if]        undo monitor mode
#   ./nethunter-wifi.sh conmode sta|monitor [ch]
#                                           switch the *internal* Wi-Fi between
#                                           normal and monitor (reloads qcacld)
#   ./nethunter-wifi.sh wifite [args...]    monitor the internal chip and run
#                                           wifite2 on wlan0 (needs setup, see
#                                           wifite-setup.sh in this pack)
#   ./nethunter-wifi.sh install [driver ...]  autoload at every boot (KernelSU)
#   ./nethunter-wifi.sh uninstall           undo install
#
# monitor/managed retype an *external* adapter's netdev in place, which is all
# ath9k_htc and friends need. That does not work for the internal Qualcomm chip:
# its mode is chosen when its driver loads, so use conmode for wlan0.
#
# conmode power-cycles the WLAN chip over PCIe/MHI, and that occasionally does
# not come back: cnss2 times out and wlan0 disappears until you reboot. It is
# intermittent, not every switch, and the script tells you when it happens.

DIR=$(cd "$(dirname "$0")" && pwd)
DEP="$DIR/modules.dep"
TMP="${TMPDIR:-/data/local/tmp}"
ERR="$TMP/.nethunter-wifi.err"
STATE="$TMP/.nethunter-wifi.loaded"
SERVICE_DIR=/data/adb/service.d
INSTALL_DIR=/data/adb/nethunter-wifi

# Modules the platform stack owns. Displacing these is what costs internal
# Wi-Fi, so it happens only when a requested driver actually needs mac80211.
#
# BOTH DDK and non-DDK packs replace the whole resident pair (peach +
# mac80211 + cfg80211). The vendor mac80211 is built without
# CONFIG_MAC80211_LEDS: its symbol table lacks
# __ieee80211_create_tpt_led_trigger and __ieee80211_get_{tx,rx,assoc,radio}_
# led_name, so every LED-using driver (ath9k_htc, mt76, rt2x00, rtl8187,
# carl9170, p54, mac80211_hwsim) dies with "Unknown symbol" while the
# resident pair stays (on-device test of run 33367477018's pack). The pack's
# cfg/mac pair is CRC-matched against this Image, so peach reloads fine
# against it and "restore" brings internal Wi-Fi back.
WIFI_SWAP_MODULES="qca_cld3_peach_v2 mac80211 cfg80211"
PLATFORM_DIR=/vendor/lib/modules
# /system/lib/modules preloads an older CAN core whose can.ko lacks
# can_sock_destruct, so pack can-raw can only load against the pack's
# can.ko. Displace the platform CAN core -- and vcan/slcan, which pin
# can-dev -- only when a requested driver actually needs it (can-raw is
# the only known consumer of the missing export).
CAN_SWAP_MODULES="can-gw can-bcm vcan slcan can-dev can"
SYSTEM_MODULES_DIR=/system/lib/modules

# The internal Qualcomm Wi-Fi driver. Its operating mode is fixed at insmod time
# by the con_mode module parameter, which qcacld declares read-only in sysfs
# (S_IRUSR), so switching means unload + reload. iw cannot retype this netdev.
QCACLD=qca_cld3_peach_v2
CONMODE_PARAM=/sys/module/$QCACLD/parameters/con_mode
CONMODE_STA=0
CONMODE_MONITOR=4

# Where the Nethunter firmware ZIP puts its blobs. ath9k_htc asks for
# ath9k_htc/htc_9271-1.4.0.fw; the firmware loader only searches /lib/firmware
# by default, which does not exist on Android, so point it at the real location.
FIRMWARE_PARAM=/sys/module/firmware_class/parameters/path
FIRMWARE_CANDIDATES="/vendor/firmware /system/etc/firmware /system/vendor/firmware /odm/firmware /lib/firmware"
FIRMWARE_PROBE=ath9k_htc/htc_9271-1.4.0.fw

DEFAULT_DRIVERS=ath9k_htc

die() { echo "error: $*" >&2; exit 1; }

need_root() {
  [ "$(id -u)" = 0 ] || die "must run as root, e.g. su -c '$0 $INVOCATION'"
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
#
# This only helps for directories the kernel can read directly. A path under
# /data is rejected with -2 under SELinux (shell_data_file is not readable by
# the firmware loader); such loads only succeed because ueventd's usermode
# helper then searches its own fixed list from /system/etc/ueventd.rc. So
# prefer the real firmware directories, which both mechanisms can reach.
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

wifi_cmd() { cmd wifi "$@" >/dev/null 2>&1; }

# Modules to unload before loading an external mac80211 driver: the whole
# resident pair plus peach. See the WIFI_SWAP_MODULES comment for why the
# vendor cfg/mac can no longer stay resident.
unload_platform_stack() {
  echo "=== displacing platform Wi-Fi stack ==="
  echo "  (internal Wi-Fi stops working until you reboot or run: $0 restore)"
  wifi_cmd set-wifi-enabled disabled
  sleep 2
  for m in $WIFI_SWAP_MODULES; do
    if resident "$m"; then
      if rmmod "$m" 2>"$ERR"; then
        echo "  unloaded $m"
      else
        echo "  could not unload $m: $(cat "$ERR" 2>/dev/null)"
      fi
    fi
  done
}

# Same for the platform CAN core when a requested driver needs can-raw: the
# /system can.ko predates can_sock_destruct, so the pack's can.ko must take
# over (and can-dev/vcan/slcan are pinned by it in both directions).
unload_can_stack() {
  echo "=== displacing platform CAN core ==="
  for m in $CAN_SWAP_MODULES; do
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

  clos=$(closure_of $drivers)
  wifi_swap=0
  can_swap=0
  for m in $clos; do
    [ "$m" = mac80211 ] && wifi_swap=1
    [ "$m" = can-raw ] && can_swap=1
  done
  [ "$wifi_swap" = 1 ] && unload_platform_stack
  [ "$can_swap" = 1 ] && unload_can_stack

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

  echo "=== restoring platform Wi-Fi + CAN stacks ==="
  # Prefer pack copies when present: the pack pair is CRC-matched against
  # this Image, and the CAN core from the pack exports can_sock_destruct.
  # Dependencies: cfg80211 <- mac80211 <- peach; can <- can-dev <- vcan/slcan.
  # Instead of hardcoding one dependency-ordered pass, iterate: insmod fails
  # harmlessly while a dependency is still missing, and later passes fill in.
  pass=0
  while [ "$pass" -lt 6 ]; do
    progress=0
    for m in cfg80211 mac80211 qca_cld3_peach_v2 can can-dev vcan slcan; do
      resident "$m" && continue
      src=""
      for dir in "$DIR" "$PLATFORM_DIR" "$SYSTEM_MODULES_DIR"; do
        if [ -f "$dir/$m.ko" ]; then src="$dir/$m.ko"; break; fi
      done
      [ -n "$src" ] || continue
      if insmod "$src" 2>"$ERR"; then
        echo "  restored $m ($(basename "$(dirname "$src")"))"
        progress=1
      fi
    done
    [ "$progress" = 0 ] && break
    pass=$((pass + 1))
  done
  for m in cfg80211 mac80211 qca_cld3_peach_v2 can can-dev vcan slcan; do
    resident "$m" || echo "  $m: not restored ($(cat "$ERR" 2>/dev/null))"
  done
  echo nethunter-inject > /sys/power/wake_unlock 2>/dev/null
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
  echo "=== internal Wi-Fi (qcacld) ==="
  if resident "$QCACLD"; then
    _c=$(conmode_now)
    echo "  con_mode ${_c:-?} -> $(conmode_name "$_c")"
  else
    echo "  $QCACLD not loaded"
  fi
  echo
  echo "=== firmware search path ==="
  cat "$FIRMWARE_PARAM" 2>/dev/null || echo "  (default)"
  echo
  echo "=== regulatory domain ==="
  # Android ships no regulatory.db, so cfg80211 falls back to world roaming
  # ("country 00"): 20 dBm everywhere, channels 12-14 no-IR, 5GHz passive-scan
  # only. Injection and beaconing work on channels 1-11 regardless.
  if command -v iw >/dev/null 2>&1; then
    iw reg get 2>/dev/null | grep -E "^(global|country)" | head -2
    echo "  (country 00 = world roaming; see README, regulatory database)"
  else
    echo "  iw not available"
  fi
}

cmd_list() {
  echo "Drivers in this pack:"
  for f in "$DIR"/*.ko; do
    [ -f "$f" ] || continue
    echo "  $(basename "$f" .ko)"
  done
}

# Put the adapter's interface into monitor mode. The interface must be down
# before the type change; iw rejects it otherwise.
cmd_monitor() {
  need_root
  command -v iw >/dev/null 2>&1 || die "iw not found"
  _iface=${1:-}
  if [ -z "$_iface" ]; then
    _iface=$(iw dev 2>/dev/null | grep -m1 -o 'Interface .*' | cut -d' ' -f2)
  fi
  [ -n "$_iface" ] || die "no wireless interface found (load a driver first)"
  _chan=${2:-}

  ip link set "$_iface" down 2>/dev/null
  if iw dev "$_iface" set type monitor 2>"$ERR"; then
    ip link set "$_iface" up 2>/dev/null
    echo "  $_iface -> monitor"
  else
    ip link set "$_iface" up 2>/dev/null
    die "could not set monitor mode: $(cat "$ERR" 2>/dev/null)"
  fi

  if [ -n "$_chan" ]; then
    iw dev "$_iface" set channel "$_chan" 2>"$ERR" \
      && echo "  channel $_chan" \
      || echo "  could not set channel $_chan: $(cat "$ERR" 2>/dev/null)"
  fi
  iw dev "$_iface" info 2>/dev/null | grep -E "type|channel|txpower"
  echo
  echo "Capture with: tcpdump -i $_iface -nn"
  echo "Back to normal: $0 managed $_iface"
}

# Undo monitor mode.
cmd_managed() {
  need_root
  command -v iw >/dev/null 2>&1 || die "iw not found"
  _iface=${1:-}
  if [ -z "$_iface" ]; then
    _iface=$(iw dev 2>/dev/null | grep -m1 -o 'Interface .*' | cut -d' ' -f2)
  fi
  [ -n "$_iface" ] || die "no wireless interface found"
  ip link set "$_iface" down 2>/dev/null
  iw dev "$_iface" set type managed 2>"$ERR" \
    && echo "  $_iface -> managed" \
    || echo "  failed: $(cat "$ERR" 2>/dev/null)"
  ip link set "$_iface" up 2>/dev/null
}

# Which qca_cld3 .ko to reload. Prefer the one in this pack: on builds with the
# monitor-mode injection patch it is the only copy that can transmit, and it is
# the copy whose symbol CRCs were built against this kernel. Fall back to the
# stock vendor module when the pack does not ship one.
qcacld_ko() {
  if have_module "$QCACLD"; then
    echo "$DIR/$QCACLD.ko"
  else
    echo "$PLATFORM_DIR/$QCACLD.ko"
  fi
}

conmode_now() { cat "$CONMODE_PARAM" 2>/dev/null; }

conmode_name() {
  case "$1" in
    "$CONMODE_STA") echo "sta (normal Wi-Fi)" ;;
    1) echo "ap" ;;
    "$CONMODE_MONITOR") echo "monitor" ;;
    5) echo "ftm" ;;
    6) echo "epping" ;;
    "") echo "(driver not loaded)" ;;
    *) echo "unknown ($1)" ;;
  esac
}

# Switch the internal chip between normal Wi-Fi and monitor mode.
#
# Unloading qcacld while Android's WifiService holds the interface fails with
# -EBUSY, so the framework is stopped first and the refcount is checked. In
# monitor mode wlan0 becomes ARPHRD_IEEE80211_RADIOTAP (type 803) and carries
# no IP -- expect adb-over-Wi-Fi to drop if that is how you are connected.
#
# Reloading qcacld power-cycles the WLAN chip over PCIe/MHI, and that does not
# always come back: cnss2 reports "MHI power up returns timeout" and
# "Failed to start MHI, err = -110", after which no wlan0 is created and no
# amount of reloading helps -- only a reboot re-establishes the link. Seen once
# in several switches on 6.6.118, so it is intermittent rather than a hard
# two-switch limit. Either way the mode is verified after the load and a wedged
# link is reported plainly rather than left to look like a script bug.
cmd_conmode() {
  need_root
  _mode=${1:-}
  _chan=${2:-}

  if [ -z "$_mode" ] || [ "$_mode" = show ]; then
    _cur=$(conmode_now)
    echo "  con_mode: ${_cur:-unset} -> $(conmode_name "$_cur")"
    [ -n "$_cur" ] && ip link show wlan0 2>/dev/null | head -1
    echo
    echo "usage: $0 conmode sta|monitor [channel]"
    return 0
  fi

  case "$_mode" in
    sta|managed|normal|0) _want=$CONMODE_STA ;;
    monitor|mon|4)        _want=$CONMODE_MONITOR ;;
    *) die "unknown mode '$_mode' (want: sta | monitor)" ;;
  esac

  _ko=$(qcacld_ko)
  [ -f "$_ko" ] || die "no $QCACLD.ko in this pack or $PLATFORM_DIR"

  _cur=$(conmode_now)
  if [ "$_cur" = "$_want" ] && [ -z "$_chan" ]; then
    echo "  already in $(conmode_name "$_want")"
    return 0
  fi

  echo "=== stopping Wi-Fi framework ==="
  wifi_cmd set-wifi-enabled disabled
  sleep 3
  ip link set wlan0 down 2>/dev/null

  if resident "$QCACLD"; then
    _refs=$(cat "/sys/module/$QCACLD/refcnt" 2>/dev/null)
    [ "${_refs:-0}" = 0 ] || echo "  warning: refcount $_refs, unload may fail"
    if rmmod "$QCACLD" 2>"$ERR"; then
      echo "  unloaded $QCACLD"
    else
      die "cannot unload $QCACLD: $(cat "$ERR" 2>/dev/null)
  something still holds it; stop Wi-Fi/hotspot/tethering and retry"
    fi
    sleep 1
  fi

  echo "=== loading $QCACLD con_mode=$_want ($(conmode_name "$_want")) ==="
  echo "  from $_ko"
  insmod "$_ko" "con_mode=$_want" 2>"$ERR" \
    || die "insmod failed: $(cat "$ERR" 2>/dev/null)"

  # The chip enumerates over PCIe/MHI asynchronously, so wlan0 appears a second
  # or two after insmod returns. Wait for it rather than racing it.
  _waited=0
  while [ "$_waited" -lt 15 ]; do
    [ -e /sys/class/net/wlan0 ] && break
    sleep 1
    _waited=$((_waited + 1))
  done

  if [ ! -e /sys/class/net/wlan0 ]; then
    echo
    echo "  no wlan0 after ${_waited}s. Kernel log:"
    dmesg | grep -iE "cnss:|peach_v2" | tail -6 | sed 's/^/    /'
    if dmesg | grep -q "MHI power up returns timeout\|Failed to start MHI"; then
      die "the WLAN chip's MHI link is wedged and only a reboot clears it.
  This is a cnss2/firmware limitation, not a problem with the module:
  reboot, then switch modes at most once per boot."
    fi
    die "driver loaded but no wlan0 appeared; see the log above"
  fi

  _got=$(conmode_now)
  [ "$_got" = "$_want" ] \
    || echo "  warning: asked for con_mode=$_want but driver reports ${_got:-unset}"

  if [ "$_want" = "$CONMODE_MONITOR" ]; then
    # Block system suspend while the ghost STA vdev is up: peach-v2 firmware
    # asserts (cmnos_assert -> MHI ramdump) when PMO suspends the target with
    # a monitor+ghost vdev active, and the driver teardown race after that
    # death took the whole kernel down (SYSTEM_LAST_KMSG: timer softirq oops
    # in __run_timers on a freed WLAN object). A wake_lock is cheap here --
    # pentest sessions are short and deliberate.
    echo nethunter-inject > /sys/power/wake_lock 2>/dev/null \
      && echo "  suspend blocked (wake_lock: nethunter-inject)"
    ip link set wlan0 up 2>/dev/null
    if [ -n "$_chan" ] && command -v iw >/dev/null 2>&1; then
      iw dev wlan0 set channel "$_chan" 2>"$ERR" \
        && echo "  channel $_chan" \
        || echo "  could not set channel $_chan: $(cat "$ERR" 2>/dev/null)"
    fi
    echo "  wlan0 type: $(cat /sys/class/net/wlan0/type 2>/dev/null) (803 = radiotap)"
    command -v iw >/dev/null 2>&1 && iw dev wlan0 info 2>/dev/null | grep -E "type|channel"
    echo
    echo "Capture with: tcpdump -i wlan0 -e -nn"
    echo "Back to normal: $0 conmode sta"
  else
    wifi_cmd set-wifi-enabled enabled
    echo nethunter-inject > /sys/power/wake_unlock 2>/dev/null
    echo "  Wi-Fi re-enabled; reconnection takes a few seconds"
    # Firmware-health check after leaving monitor mode. The driver logs a
    # marker when the ghost-vdev teardown could not reach firmware (WMI
    # unavailable or VDEV_DELETE send failed); a leaked ghost vdev asserts
    # peach-v2 firmware on the NEXT system suspend and the teardown race
    # panics the kernel (SYSTEM_LAST_KMSG of a 6.6.118 boot). Say so now
    # instead of letting the phone die 10-20 minutes later.
    if dmesg 2>/dev/null | grep -q "ghost vdev leaked\|teardown skipped, WMI unavailable"; then
      echo
      echo "  WARNING: the monitor-mode helper vdev could not be torn down"
      echo "  in firmware. The phone will crash on the next deep sleep."
      echo "  === REBOOT NOW (normal restart) to clear it. ==="
    fi
  fi
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

# ---------------------------------------------------------------------------
# Wifite support.
#
# wifite2 (kimocoder fork) drives aircrack-ng with a monitor interface. On
# this device the monitor interface is the internal chip's own wlan0 after a
# con_mode=4 driver reload -- there is no separate mon0 and no adapter to
# airmon-ng. This wrapper:
#   1. finds a usable Python 3 + wifite2 (Termux prefix or NetHunter chroot),
#   2. switches the internal chip to monitor mode via cmd_conmode,
#   3. launches wifite pointed at wlan0, passing any extra arguments through.
# On exit it flips the chip back to sta so the phone regains its Wi-Fi even
# after Ctrl-C.
#
# wifite2's own interface detection expects interface names like wlan0mon;
# the kimocoder fork also accepts plain monitor-mode interfaces, and --iface
# (or -i) pins it explicitly, so wlan0 is handed to it directly.
# ---------------------------------------------------------------------------
WIFITE_DIRS="/data/data/com.termux/files/usr/bin /root/nethunter/bin /usr/local/bin /usr/bin"
WIFITE_IFACE=wlan0

find_wifite() {
  for d in $WIFITE_DIRS; do
    [ -x "$d/wifite" ] && { echo "$d/wifite"; return 0; }
  done
  # chroot layouts: search the common NetHunter/Kali roots via the termux
  # proot paths too, but only report executables that exist.
  return 1
}

find_python_for_wifite() {
  if [ -x /data/data/com.termux/files/usr/bin/python3 ]; then
    echo /data/data/com.termux/files/usr/bin/python3
    return 0
  fi
  command -v python3 2>/dev/null && return 0
  return 1
}

cmd_wifite() {
  need_root
  # Droidspaces container install takes priority when present: the Ubuntu
  # container carries the full toolchain (aircrack-ng, reaver, bully,
  # pixiewps, hcxdumptool, hostapd) with none of Termux's repo gaps.
  DS_BIN=/data/local/Droidspaces/bin/droidspaces
  if [ -x /mnt/Droidspaces/idk/usr/bin/wifite ] && [ -x "$DS_BIN" ]; then
    CPID=$("$DS_BIN" pid idk 2>/dev/null)
    [ -n "$CPID" ] || die "container 'idk' is not running; start it (Droidspaces app or $DS_BIN start)"
    _wifite_mode=container
  else
    WIFITE_BIN=$(find_wifite) || die "wifite not found; install it with wifite-setup.sh (in this pack), then re-run: $0 wifite"
    PYTHON=$(find_python_for_wifite) || die "python3 not found"
    _wifite_mode=host
  fi

  # Internal chip: switch to con_mode=4 unless it is already there. External
  # adapters loaded via 'load' keep working untouched.
  _cur=$(conmode_now)
  if [ "$_cur" != "$CONMODE_MONITOR" ]; then
    cmd_conmode monitor || true
    _cur=$(conmode_now)
    [ "$_cur" = "$CONMODE_MONITOR" ] || die "internal chip did not enter monitor mode; fix conmode first ($0 conmode monitor)"
  fi
  ip link set "$WIFITE_IFACE" up 2>/dev/null

  echo "=== launching wifite on $WIFITE_IFACE ($_wifite_mode) ==="
  # TERM is needed by wifite's curses-free output; PATH additions cover the
  # Termux toolchain (aircrack-ng, iw, ...) when run from adb shell su.
  TERM=${TERM:-dumb}
  if [ "$_wifite_mode" = container ]; then
    # wlan0 lives on the host; the container shares the host network
    # namespace (net_mode=host), so its tools see the monitor iface as-is.
    nsenter -t "$CPID" -m -p -- /bin/bash -c \
      "export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin TERM=$TERM; wifite -i $WIFITE_IFACE $*"
    _rc=$?
  else
    if [ -d /data/data/com.termux/files/usr/bin ]; then
      PATH="/data/data/com.termux/files/usr/bin:$PATH"
      export PATH TERM PREFIX=/data/data/com.termux/files/usr TMPDIR=/data/data/com.termux/files/usr/tmp
    fi
    "$PYTHON" "$WIFITE_BIN" -i "$WIFITE_IFACE" "$@"
    _rc=$?
  fi

  # Regain normal Wi-Fi even on Ctrl-C: wifite cannot restore what it did
  # not set up (it never touched the con_mode state machine).
  echo
  echo "=== returning internal chip to sta ==="
  cmd_conmode sta || echo "  restore failed; run: $0 conmode sta"
  return "$_rc"
}

cmd_help() {
  # Print the usage comment block: from the "Usage" heading up to the last
  # line before the first non-comment line, dropping that line itself.
  sed -n '/^# Usage, run as root/,/^[^#]/{/^[^#]/d;p;}' "$0" \
    | sed -e 's/^# \{0,1\}//'
}

# Interactive menu, shown when the script is run with no arguments.
#
# Every entry maps onto a subcommand that also works non-interactively, so the
# menu is a convenience and never the only way to reach something. It re-reads
# the live state each pass rather than caching it, because the modes it offers
# change what is true underneath -- and because a wedged MHI link means the
# state after an action is not always the state that was asked for.
menu_state() {
  if resident "$QCACLD"; then
    _c=$(conmode_now)
    echo "internal Wi-Fi: $(conmode_name "$_c")"
  else
    echo "internal Wi-Fi: $QCACLD not loaded"
  fi
}

cmd_menu() {
  # read -r is POSIX; toybox sh supports it. No `read -p`, that is a bashism.
  while :; do
    echo
    echo "=============================================="
    echo " nethunter-wifi -- $(menu_state)"
    echo "=============================================="
    echo "  1) internal Wi-Fi -> monitor mode"
    echo "  2) internal Wi-Fi -> normal (sta)"
    echo "  3) load an external adapter driver"
    echo "  4) external adapter -> monitor mode"
    echo "  5) external adapter -> managed mode"
    echo "  6) status"
    echo "  7) list drivers in this pack"
    echo "  8) restore platform Wi-Fi stack"
    echo "  9) wifite on the internal chip"
    echo "  h) help"
    echo "  0) quit"
    echo
    printf 'choice: '
    read -r _sel || { echo; return 0; }

    case "$_sel" in
      1)
        printf 'channel (blank = leave as is): '
        read -r _ch
        cmd_conmode monitor "$_ch"
        ;;
      2) cmd_conmode sta ;;
      3)
        cmd_list
        printf 'driver (blank = %s): ' "$DEFAULT_DRIVERS"
        read -r _drv
        cmd_load ${_drv:-$DEFAULT_DRIVERS}
        ;;
      4)
        printf 'interface (blank = auto-detect): '
        read -r _if
        printf 'channel (blank = leave as is): '
        read -r _ch
        cmd_monitor "$_if" "$_ch"
        ;;
      5)
        printf 'interface (blank = auto-detect): '
        read -r _if
        cmd_managed "$_if"
        ;;
      6) cmd_status ;;
      7) cmd_list ;;
      8) cmd_restore ;;
      9) cmd_wifite ;;
      h|H) cmd_help ;;
      0|q|quit|exit) return 0 ;;
      "") ;;
      *) echo "  no such choice: $_sel" ;;
    esac
  done
}


# No arguments means the interactive menu -- but only when there is a terminal
# to prompt at. A scripted caller with no arguments gets the help text rather
# than a menu that immediately reads EOF, or a surprise driver load.
if [ $# -eq 0 ]; then
  if [ -t 0 ]; then
    action=menu
  else
    action=help
  fi
else
  action=$1
fi
# Kept for the need_root message: the subcommand's own args are shifted away
# before it runs, so reconstruct the full invocation here.
INVOCATION=${*:-$action}
[ $# -gt 0 ] && shift
case "$action" in
  menu)      cmd_menu ;;
  load)      cmd_load "$@" ;;
  restore)   cmd_restore ;;
  status)    cmd_status ;;
  list)      cmd_list ;;
  monitor)   cmd_monitor "$@" ;;
  managed)   cmd_managed "$@" ;;
  conmode)   cmd_conmode "$@" ;;
  wifite)    cmd_wifite "$@" ;;
  install)   cmd_install "$@" ;;
  uninstall) cmd_uninstall ;;
  -h|--help|help)
    cmd_help
    ;;
  *)
    die "unknown command '$action' (try: $0 --help)"
    ;;
esac
