# OnePlus 13 Kernel Builder

[![Build OnePlus 13 Kernel](https://github.com/Hipuu/OnePlus13-KernelBuilder/actions/workflows/build-oneplus13-kernel.yml/badge.svg)](https://github.com/Hipuu/OnePlus13-KernelBuilder/actions/workflows/build-oneplus13-kernel.yml)

GitHub Actions workflow that builds custom **OnePlus 13** (SM8750 / "sun") kernels with KernelSU / KernelSU-Next, SUSFS, and NetHunter wireless drivers.

Built on the [WildKernels/OnePlus_KernelSU_SUSFS](https://github.com/WildKernels/OnePlus_KernelSU_SUSFS) pipeline, vendored at a pinned commit and specialized to a single device. Produces a flashable AnyKernel3 ZIP, a raw kernel `Image`, and a loadable wireless-module pack.

## Features

| | |
|---|---|
| **Root** | KernelSU-Next (`KSUN`) or KernelSU (`KSU`), ref resolved to a commit SHA before build |
| **SUSFS** | Version auto-detected from the branch (`v2.2.0` on `gki-android15-6.6`), with upstream patch/rej-fix logic |
| **Wireless** | External Wi-Fi adapters as loadable modules, with a dependency-resolving loader script |
| **NetHunter** | Bluetooth (HCIBTUSB, BCM203X, BPA10X, BFUSB), SDR (AirSpy, HackRF), full CAN stack, USB serial (CH341, FTDI, PL2303) |
| **Scheduler** | HMBIRD (Fengchi) patches for SM8750 |
| **Networking** | BBR, BBRv3, TTL target, IP_SET |
| **Other patches** | NTSync, Unicode fix, Droidspaces, module intercept/overlay, vendor-module debloat, memory/VFS/scheduler optimizations |
| **Toolchain** | Pinned ZyC Clang 19 or manifest Clang; `O2`/`O3`; `thin`/`full`/`none` LTO |
| **CI** | Six kernel variants buildable in parallel, ccache-accelerated, artifact / prerelease / release publishing |

### Wireless adapter support

Drivers are built as modules and shipped in `kernel_modules_*.zip`:

| Vendor | Drivers |
|--------|---------|
| Atheros | `ath9k_htc`, `ath10k_usb`, `carl9170` |
| Realtek | `rtl8187`, `rtl8xxxu`, `rtw88` (out-of-tree, [lwfinger/rtw88](https://github.com/lwfinger/rtw88)) |
| Ralink | `rt2500usb`, `rt73usb`, `rt2800usb` |
| MediaTek | `mt7601u`, `mt76x0u`, `mt76x2u`, `mt7921u` |
| Zydas / Intersil | `zd1211rw`, `p54usb` |
| Virtual | `mac80211_hwsim` |

## Not included

- **No boot.img / vendor_boot / DLKM images.** Output is a raw `Image`, an AnyKernel3 ZIP, and a modules ZIP.
- **No other devices.** OnePlus 13 only.
- **No external USB Wi-Fi adapters on the DDK build.** See [DDK build](#ddk-build).

## DDK build

`OP13-6.6.118` builds with Bazel/Kleaf instead of `make`, and additionally
produces OnePlus's own Wi-Fi driver, `qca_cld3_peach_v2.ko`, as a DDK
(`ddk_module`) external module. Because it is compiled against the same
`Module.symvers` as the shipped `Image`, it passes the CRC check that the
stock module fails on a patched kernel — which is what makes monitor mode on
the **internal** Wi-Fi possible.

Monitor mode itself needs no patch: `CONFIG_FEATURE_MONITOR_MODE_SUPPORT`,
`CONFIG_QCA_MONITOR_PKT_SUPPORT` and `CONFIG_WIFI_MONITOR_SUPPORT` are already
enabled in the stock `sun_gki_peach-v2_defconfig` and gated at runtime by the
`con_mode` module parameter. Injection does require a patch; see
[monitor-mode injection](#monitor-mode-injection).

> [!IMPORTANT]
> The DDK build and the external-adapter module pack are mutually exclusive.
> `msm-kernel` declares `cfg80211.ko` and `mac80211.ko` among its in-tree
> modules, and the vendor kernel is a mixed build on top of GKI, so adding the
> same two to the GKI defconfig makes `modpost` fail with `'wiphy_new_nm'
> exported twice`. Only one copy can exist. The DDK build keeps the platform
> pair, since that is what `qca_cld3_peach_v2.ko` is CRC-matched against, so
> `ath9k_htc` and the other external drivers are not built on this variant.
> This is the same incompatibility described in
> [why the drivers are not built in](#why-the-drivers-are-not-built-in),
> reached at build time rather than on device.

Build the other five variants for external adapter support, or set `ddk` to
`false` to build `6.6.118` the `make` way.

The module pack on this variant contains `qca_cld3_peach_v2.ko`, the platform
`cfg80211`/`mac80211`/`rfkill` it links against, the full dependency closure
that `modules.dep` resolves (about 60 modules), and the CAN and USB-serial
drivers. The build fails if the shipped modules' `vermagic` does not match the
`Image`, since a mismatch makes them unloadable.

### Monitor-mode injection

`ddk_injection` patches `wlan_mon_drv_ops` with a `.ndo_start_xmit` handler
that strips the radiotap header and calls `wlan_cfg80211_mgmt_tx()`. Upstream
omits Tx from monitor mode entirely — the comment reads *"does not Tx and most
of operations"* — so without this a radiotap frame written to the monitor
interface is dropped by the network stack before the driver sees it.

> [!WARNING]
> This is **experimental and unverified on hardware.** The firmware decides
> whether to accept a frame on a monitor vdev and may drop anything that is not
> a management frame, or drop everything, in which case the handler is dead
> code. Radiotap Tx flags (rate, retries, `TX_NOACK`) are parsed only far enough
> to locate the 802.11 frame and are otherwise ignored. The 20 dBm world
> regulatory cap and the `no IR` flag on channels 12–14 still apply.


## Supported variants

| Model | Kernel | OS | Build | Manifest |
|-------|--------|----|-------|----|
| `OP13-6.6.89` | 6.6.89 | A16 | make | `oneplus_13_6.6.89_w.xml` |
| `OP13-6.6.118` | 6.6.118 | A16 | **Bazel + DDK** | `oneplus_13_6.6.118_w.xml` |
| `OP13-6.6.66` | 6.6.66 | A15 | make | `oneplus_13_6.6.66_v.xml` |
| `OP13-6.6.30` | 6.6.30 | A15 | make | `oneplus_13_6.6.30_v.xml` |
| `OP13-CPH-6.6.89` | 6.6.89 | A15 (global) | make | `oneplus_13_global_6.6.89_v.xml` |
| `OP13-CPH-6.6.56` | 6.6.56 | A15 (global) | make | `oneplus_13_global_6.6.56_v.xml` |

All share the same SoC (Snapdragon 8 Elite / SM8750), Android version (`android15`), and manifest branch (`wild/sm8750`). Only `OP13-6.6.118` carries the Kleaf/Bazel projects in its manifest; see [DDK build](#ddk-build).

## Usage

Open the **Actions** tab, select **Build OnePlus 13 Kernel**, and click **Run workflow**. Or from the CLI:

```bash
gh workflow run "Build OnePlus 13 Kernel" -f kernel_version="6.6.118 A16"
```

### Inputs

| Input | Description | Default |
|-------|-------------|---------|
| `kernel_version` | Variant to build; `all` builds every variant in parallel | `6.6.89 A16` |
| `ksu_variant` | `KSUN` or `KSU` | `KSUN` |
| `ksu_branch` | KernelSU branch/tag/commit (empty = `dev` for KSUN, `main` for KSU) | empty |
| `use_susfs` | Enable SUSFS | `true` |
| `susfs_branch` | SUSFS branch/commit (empty = `gki-android15-6.6`) | empty |
| `nethunter` | Enable NetHunter inline configs | `true` |
| `wireless_modules` | Build and package wireless kernel modules | `true` |
| `optimize_level` | `O2` or `O3` | `O2` |
| `lto` | `thin`, `full`, or `none` | `thin` |
| `compiler` | Pinned ZyC Clang 19 or manifest Clang | `zycromerz-19` |
| `use_opt_patches` | Apply optimization patches | `true` |
| `ddk` | Build with Bazel/Kleaf and produce the qcacld-3.0 DDK module (`6.6.118 A16` only) | `true` |
| `ddk_injection` | Patch qcacld monitor mode for frame injection (experimental) | `true` |
| `kernel_uname` | uname suffix | `OP-WILD` |
| `build_timestamp` | Custom uname timestamp (empty = current UTC) | empty |
| `clean_build` | Build without ccache restore | `false` |
| `release_type` | `none`, `prerelease`, or `release` | `none` |
| `debug` | Build modules and upload debug artifacts | `false` |

### Artifacts

| File | Contents |
|------|----------|
| `AK3_<MODEL>_<OS>_<KERNEL>_<KSU>_<VER>.zip` | AnyKernel3 flashable package |
| `kernel_modules_<MODEL>_<OS>_<KERNEL>.zip` | Wireless/CAN modules, `modules.dep`, `nethunter-wifi.sh` |
| `Nethunter-Wireless-Firmware-<VER>.zip` | Firmware blobs (passthrough) |
| `Image_<MODEL>_<KERNEL>` | Raw ARM64 kernel image |

## Installation

1. Flash the AnyKernel3 ZIP via custom recovery, KernelSU, APatch, or another flasher.
2. Reboot and install the matching manager app — [KernelSU-Next](https://github.com/KernelSU-Next/KernelSU-Next/releases) or [KernelSU](https://github.com/tiann/KernelSU/releases).

## Wireless modules

> [!IMPORTANT]
> Modules are built with `CONFIG_MODVERSIONS=y`. A module pack loads **only** on the exact kernel build it shipped with — matching base versions is not sufficient.

Extract `kernel_modules_*.zip` on the device, extract the firmware ZIP, and run the bundled loader as root:

```bash
./nethunter-wifi.sh load            # load default driver (ath9k_htc)
./nethunter-wifi.sh load mt76x2u    # or any driver in the pack
./nethunter-wifi.sh list            # list available drivers
./nethunter-wifi.sh status          # resident modules, PHYs, regdomain
./nethunter-wifi.sh monitor         # monitor mode, auto-detected interface
./nethunter-wifi.sh monitor "" 6    # monitor mode on channel 6
./nethunter-wifi.sh managed         # leave monitor mode
./nethunter-wifi.sh conmode monitor 6   # internal Wi-Fi -> monitor, channel 6
./nethunter-wifi.sh conmode sta     # internal Wi-Fi -> normal
./nethunter-wifi.sh conmode         # show the current mode
./nethunter-wifi.sh restore         # unload and restore internal Wi-Fi
./nethunter-wifi.sh install         # autoload at boot (KernelSU/Magisk)
./nethunter-wifi.sh uninstall       # remove boot service
```

`load` walks the shipped `modules.dep` to resolve dependency order, displaces the platform Wi-Fi stack when the driver requires `mac80211`, and points the firmware loader at the directory holding the NetHunter blobs.

### Internal Wi-Fi monitor mode (`conmode`)

`monitor`/`managed` retype an external adapter's netdev in place. The internal Qualcomm chip cannot be retyped that way — qcacld fixes its role at load time from the `con_mode` module parameter, which is read-only in sysfs. `conmode` reloads the driver with the value you ask for:

| `con_mode` | Mode |
|-----------|------|
| `0` | STA — normal Wi-Fi (default) |
| `1` | AP |
| `4` | Monitor |
| `5` | FTM (factory test) |
| `6` | EPPING |

Verified on a OnePlus 13 running the DDK build: `wlan0` becomes `ARPHRD_IEEE80211_RADIOTAP` (type `803`) and `tcpdump -i wlan0 -e -nn` captures beacons, data frames, RTS/CTS and block-ACKs from all nearby networks with radiotap RSSI and rate annotations.

> [!WARNING]
> `conmode` power-cycles the WLAN chip over PCIe/MHI, and the link occasionally fails to come back — `cnss2` logs `MHI power up returns timeout` / `Failed to start MHI, err = -110`, and `wlan0` stays gone until you reboot. The script detects this and says so. It is intermittent, not every switch.

### Behavior to expect

- **Loading a `mac80211` driver disables the internal Wi-Fi.** This is unavoidable on this device — see [why the drivers are not built in](#why-the-drivers-are-not-built-in). Drivers that do not pull in `mac80211` (for example `slcan`) leave it alone.
- **`restore` reloads the platform modules, but Android's `WifiService` usually stays latched in a failed state.** Reboot to get internal Wi-Fi back reliably.
- **`install` costs the internal Wi-Fi on every boot.** Undo with `uninstall`.
- **The radio runs in the world regulatory domain.** See [regulatory database](#regulatory-database).

### Tested adapters

| Adapter | USB ID | Driver | Result |
|---------|--------|--------|--------|
| Atheros AR9271 | `040d:3801` | `ath9k_htc` | Firmware load, `phy0`, managed + **monitor** + AP modes, frame capture confirmed |

Verified against the 6.6.118 A16 build. Reported PHY capabilities: IBSS, managed, AP, AP/VLAN, monitor, P2P-client, P2P-GO.

### Regulatory database

Android ships no `regulatory.db`, so `cfg80211` logs `failed to load regulatory.db` and falls back to the world domain (`country 00`):

- 20 dBm cap on all channels
- Channels 12–14 flagged `no IR` — injection and beaconing blocked there
- 5 GHz entirely `PASSIVE-SCAN`
- `iw reg set <CC>` is accepted but has no effect

Channels 1–11 remain fully usable, injection included.

To lift the restriction, install `regulatory.db` and `regulatory.db.p7s` from [wireless-regdb](https://git.kernel.org/pub/scm/linux/kernel/git/sforshee/wireless-regdb.git) into a directory `ueventd` searches. A KernelSU/Magisk module overlaying `system/etc/firmware/` is the practical route — the same mechanism the NetHunter firmware ZIP uses.

<details>
<summary>Why setting <code>firmware_class/parameters/path</code> is not enough</summary>

A direct kernel load from `/data` fails with `-2` under SELinux (`shell_data_file` is not readable by the firmware loader). Such loads only succeed because ueventd's usermode helper subsequently searches its own fixed list, defined in `/system/etc/ueventd.rc`:

```
/etc/firmware/  /odm/firmware/  /data/vendor/firmware/update/  /vendor/firmware/
/firmware/image/  /vendor/firmware_mnt/image/qca6490/  /data/oplus/fw_update/
/mnt/vendor/persist/copy/  /mnt/vendor/persist/  /odm/etc/wifi/  /vendor/firmware_mnt/image/
```

The blob must land in one of those paths.
</details>

### Why the drivers are not built in

`ath9k_htc` is `depends on USB && MAC80211`, and Kconfig forbids a built-in driver depending on a modular provider. `CONFIG_ATH9K_HTC=y` therefore forces `CONFIG_MAC80211=y`, `CONFIG_CFG80211=y` and `CONFIG_RFKILL=y`.

That is fatal on this device. OnePlus builds its own `cfg80211` in a separate tree with different symbol CRCs — the bundled `mac80211` already refuses to load against the platform one (`disagrees about version of symbol wiphy_new_nm`). With `cfg80211` compiled into the Image, `/vendor/lib/modules/cfg80211.ko` cannot load at all, `qca_cld3_peach_v2` fails its CRC check on every boot, and the internal Wi-Fi is permanently dead.

Keeping the drivers modular confines that cost to the moment the loader runs, and a reboot undoes it.

<details>
<summary>Manual load procedure (what the loader automates)</summary>

Both `cfg80211.ko` **and** `mac80211.ko` must come from the pack — the platform pair is CRC-incompatible with these drivers.

```bash
# Turn Wi-Fi off in Settings first, then unload the stock stack
rmmod qca_cld3_peach_v2
rmmod cfg80211

# Load the bundled stack
insmod ./cfg80211.ko
insmod ./mac80211.ko

# Load the driver chain (example: ath9k_htc)
insmod ./ath.ko
insmod ./ath9k_hw.ko
insmod ./ath9k_common.ko
insmod ./ath9k_htc.ko
```

See EmberHeart's `docs/drivers.md` for per-driver dependency chains.
</details>

## How it works

The OnePlus 13 kernel needs WildKernels' manifest fork, pinned source/toolchain revisions, and a specific patch ordering. Rather than reimplement ~2400 lines of that pipeline and risk drift, this repository vendors it from commit `bfe12144` (with `WildKernels/kernel_patches` pinned to `24865a0`) and changes only what standalone single-device operation requires:

- Internal sub-action references point at this repository's copies.
- Source sync pulls pinned Clang, kernel build-tools and AnyKernel3 from WildKernels' public `toolchain-cache` release, so no per-repository toolchain mirror is needed.
- A thin workflow exposes only OnePlus 13 options.
- The KernelSU ref is resolved to a commit SHA before building. KernelSU-Next's `setup.sh` checks out the *latest tag* when given no argument, and that tag lags the SUSFS patch set — `10_enable_susfs_for_ksu.patch` expects a `kernel/Kconfig` that only exists on `dev`, leaving an unfixable `kernel/Kconfig.rej`.

See [TESTING.md](TESTING.md) for validation, build profiles, and debugging.

## Repository layout

```
.github/
  workflows/build-oneplus13-kernel.yml    # multi-version matrix workflow
  actions/
    build-kernel/                         # vendored build pipeline + NetHunter extensions
      files/nethunter-wifi.sh             # on-device module loader
    kernel-source-sync/                   # vendored source/toolchain sync
    cache/{restore,save}/                 # release-backed ccache helpers
configs/
  OP13-6.6.{89,118,66,30}.json            # device configs (A16, A15)
  OP13-CPH-6.6.{89,56}.json               # global device configs (A15)
manifests/
  a16/oneplus_13_6.6.{89,118}_w.xml       # pinned manifests (OxygenOS 16)
  a15/oneplus_13_6.6.{66,30}_v.xml        # pinned manifests (OxygenOS 15)
  a15/oneplus_13_global_6.6.{89,56}_v.xml # pinned global manifests (OxygenOS 15)
validate_workflow.sh                      # local static validation
```

## Credits

- Build pipeline, patches, manifest and toolchain cache — [WildKernels/OnePlus_KernelSU_SUSFS](https://github.com/WildKernels/OnePlus_KernelSU_SUSFS), [WildKernels/kernel_patches](https://github.com/WildKernels/kernel_patches)
- NetHunter wireless drivers, module packaging, firmware passthrough, and workflow controls — [nullptr-t-oss/EmberHeart_OnePlus11](https://github.com/nullptr-t-oss/EmberHeart_OnePlus11)
- Out-of-tree rtw88 drivers — [lwfinger/rtw88](https://github.com/lwfinger/rtw88)
- NetHunter Wireless Firmware — [nullptr-t-oss/Nethunter-Wireless-Firmware](https://github.com/nullptr-t-oss/Nethunter-Wireless-Firmware)
- [KernelSU](https://github.com/tiann/KernelSU) · [KernelSU-Next](https://github.com/KernelSU-Next/KernelSU-Next) · [SUSFS](https://gitlab.com/simonpunk/susfs4ksu) · [AnyKernel3](https://github.com/osm0sis/AnyKernel3) · [OnePlusOSS](https://github.com/OnePlusOSS)

## License

Workflow configuration is provided as-is. Kernel source and patches retain their upstream licenses (Linux kernel GPL-2.0, KernelSU GPL-3.0, SUSFS GPL-2.0).

## Security

Never commit personal access tokens. Releases use the automatically provided `GITHUB_TOKEN`. If you pasted a PAT anywhere while setting this up, revoke it at https://github.com/settings/tokens.
