#!/system/bin/sh
# wifite-setup.sh -- install the wifite2 toolchain for the INTERNAL Wi-Fi chip.
#
# Wifite is not a prebuilt Android app: it is a Python driver for the
# aircrack-ng suite. This script installs that toolchain into the two
# environments that work on a rooted phone:
#
#   1. Termux  (recommended: no chroot, direct access to /sys/class/net)
#   2. Kali NetHunter chroot (everything prebuilt via apt)
#
# Run it INSIDE the target environment:
#   Termux:            sh wifite-setup.sh
#   NetHunter chroot:  sh wifite-setup.sh
#
# Termux package sources used:
#   main      aircrack-ng-less deps: python, git, libpcap, openssl, make/clang
#   root-repo iw, macchanger, dnsmasq, pixiewps
#   tur-hacking (TUR) aircrack-ng, reaver, hashcat, mdk4
#   source builds (best effort): hcxdumptool, hcxtools, bully
#
# After it finishes, from the module pack directory (as root, e.g. adb shell):
#   ./nethunter-wifi.sh wifite            # monitor chip + launch wifite
# or inside Termux directly:
#   wifite -i wlan0                       # after: ./nethunter-wifi.sh conmode monitor
#
# Nothing here needs to be re-run after a kernel or pack update.

set -u

PKG=""
APT=""
if command -v pkg >/dev/null 2>&1 && [ -n "${PREFIX:-}" ] && [ "${PREFIX#*/com.termux}" != "$PREFIX" ]; then
    PKG=pkg
elif command -v apt-get >/dev/null 2>&1 && [ -f /etc/kali-release ]; then
    APT=apt-get
else
    echo "error: run this script inside Termux or the NetHunter (Kali) chroot" >&2
    exit 1
fi

MISSING=""

note() { echo "[*] $*"; }
ok()   { echo "[+] $*"; }
warn() { echo "[!] $*"; }

have() { command -v "$1" >/dev/null 2>&1; }

# --- 1. Core packages -------------------------------------------------------

if [ -n "$PKG" ]; then
    note "installing repos and core packages (Termux)"
    pkg install -y root-repo tur-repo git python libpcap openssl \
        clang make pkg-config >/dev/null 2>&1 || warn "core pkg install had warnings"
    # tur-hacking is a TUR component that is not subscribed by tur-repo by
    # default; add its source line explicitly next to the others.
    TUR_LIST="$PREFIX/etc/apt/sources.list.d/tur.list"
    if [ -f "$TUR_LIST" ] && ! grep -q "tur-hacking" "$TUR_LIST"; then
        echo "deb https://tur.kcubeterm.com tur-packages tur-hacking" >> "$TUR_LIST"
        note "subscribed tur-hacking component"
    fi
    pkg update >/dev/null 2>&1 || true
    pkg install -y aircrack-ng reaver hashcat mdk4 iw macchanger dnsmasq \
        pixiewps >/dev/null 2>&1 || warn "some pentest packages failed to install"
elif [ -n "$APT" ]; then
    note "installing packages (Kali chroot)"
    apt-get update >/dev/null 2>&1 || true
    apt-get install -y aircrack-ng reaver bully pixiewps hcxdumptool hcxtools \
        hashcat hostapd dnsmasq macchanger iw python3 wifite >/dev/null 2>&1 \
        || warn "some packages failed to install"
fi

# --- 2. Source builds for tools with no Termux package ----------------------

build_from_git() {
    # $1=name $2=repo url
    _name=$1
    _url=$2
    have "$_name" && return 0
    if [ -z "$PKG" ]; then
        MISSING="$_name $MISSING"
        return 0
    fi
    note "building $_name from source (not packaged for Termux)"
    _dir="$HOME/$_name"
    if [ ! -d "$_dir" ]; then
        git clone --depth 1 "$_url" "$_dir" >/dev/null 2>&1 || {
            warn "git clone failed: $_url"
            MISSING="$_name $MISSING"
            return 0
        }
    fi
    ( cd "$_dir" && make -j4 >/dev/null 2>&1 \
        && make install PREFIX="$PREFIX" >/dev/null 2>&1 ) \
        && ok "$_name installed" \
        || { warn "$_name build failed"; MISSING="$_name $MISSING"; }
}

build_from_git hcxdumptool https://github.com/ZerBea/hcxdumptool.git
build_from_git hcxtools     https://github.com/ZerBea/hcxtools.git
build_from_git bully        https://github.com/aircrack-ng/bully.git

# hostapd: wifite2's evil-twin fake AP. Not packaged for Termux, and the
# default defconfig builds a useless no-driver binary, so enable the nl80211
# driver against libnl from the Termux repo and cross-build for the prefix.
if ! have hostapd && [ -n "$PKG" ]; then
    note "building hostapd from source (not packaged for Termux)"
    pkg install -y libnl >/dev/null 2>&1 || warn "libnl install failed"
    _hver=2.11
    _hdir="$HOME/hostapd-$_hver"
    if [ ! -x "$_hdir/hostapd/hostapd" ]; then
        curl -sL "https://w1.fi/releases/hostapd-$_hver.tar.gz" \
            | tar xz -C "$HOME" 2>/dev/null || warn "hostapd tarball fetch failed"
    fi
    if [ -d "$_hdir/hostapd" ]; then
        (
            cd "$_hdir/hostapd" || exit 1
            cp defconfig .config
            sed -i -e 's/^#CONFIG_DRIVER_NL80211=y/CONFIG_DRIVER_NL80211=y/' \
                   -e 's/^#CONFIG_LIBNL32=y/CONFIG_LIBNL32=y/' .config
            printf 'CFLAGS += -I%s/include -I%s/include/libnl3\nLDFLAGS += -L%s/lib\n' \
                "$PREFIX" "$PREFIX" "$PREFIX" >> .config
            make -j4 CC=clang BINDIR="$PREFIX/bin" >/dev/null 2>&1 \
                && make install BINDIR="$PREFIX/bin" >/dev/null 2>&1
        ) && ok "hostapd installed" || warn "hostapd build failed (evil-twin AP unavailable)"
    else
        warn "hostapd source missing; evil-twin AP unavailable"
    fi
fi

# --- 3. wifite2 itself ------------------------------------------------------

if ! have wifite && ! [ -x "$HOME/wifite2/wifite.py" ]; then
    note "cloning wifite2 (kimocoder fork)"
    git clone --depth 1 https://github.com/kimocoder/wifite2 "$HOME/wifite2" \
        >/dev/null 2>&1 || warn "wifite2 clone failed"
fi
if have wifite; then
    ok "wifite on PATH: $(command -v wifite)"
elif [ -x "$HOME/wifite2/wifite.py" ]; then
    ln -sf "$HOME/wifite2/wifite.py" "$PREFIX/bin/wifite" 2>/dev/null \
        || ln -sf "$HOME/wifite2/wifite.py" /usr/local/bin/wifite 2>/dev/null
    have wifite && ok "wifite linked: $(command -v wifite)" \
        || warn "run wifite via: python3 $HOME/wifite2/wifite.py"
else
    MISSING="wifite2 $MISSING"
fi

# --- 4. Report --------------------------------------------------------------

echo
echo "=== toolchain status ==="
for t in python3 aircrack-ng aireplay-ng airodump-ng packetforge-ng iw \
         macchanger reaver pixiewps bully hcxdumptool hcxpcapngtool hashcat \
         hostapd dnsmasq wifite; do
    have "$t" && ok "$t" || warn "missing: $t"
done

if [ -n "$MISSING" ]; then
    echo
    warn "failed or unavailable: $MISSING"
    warn "wifite still runs without the missing optional tools"
fi

cat <<EOF

=== next steps ===
1. From the module pack directory (as root):
       ./nethunter-wifi.sh wifite
   This switches the internal chip to monitor mode, launches wifite on
   wlan0, and restores normal Wi-Fi when wifite exits.
2. Manual flow (equivalent):
       ./nethunter-wifi.sh conmode monitor
       wifite -i wlan0
       ./nethunter-wifi.sh conmode sta
3. Country code: stock "00" limits scanning to ch 1-11. For 5 GHz targets:
       su -c 'cmd wifi force-country-code enabled US'
   (re-apply after each conmode switch)
EOF
