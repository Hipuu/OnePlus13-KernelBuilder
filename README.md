# OnePlus 13 Kernel Builder

A GitHub Actions workflow that builds custom **OnePlus 13** kernels with KernelSU / KernelSU-Next, SUSFS, and Nethunter wireless drivers. It reuses the proven, actively maintained build pipeline from [WildKernels/OnePlus_KernelSU_SUSFS](https://github.com/WildKernels/OnePlus_KernelSU_SUSFS), pinned to a specific commit and specialized to the OnePlus 13 only.

## Why it is built this way

The OnePlus 13 (SM8750 / "sun") kernel is built with WildKernels' manifest fork, pinned source/toolchain revisions, and its maintained patch pipeline. Rather than re-implement ~2400 lines of patch ordering and risk drift, this repository vendors the upstream build logic from commit `bfe12144`, pins `WildKernels/kernel_patches` to commit `24865a0`, and changes only what is required to run it standalone for one device:

- The composite `build-kernel` action's internal sub-action references are pointed at this repository's copies.
- The source-sync step downloads the pinned Clang, kernel build-tools and AnyKernel3 archives from WildKernels' public `toolchain-cache` release, so no per-repository toolchain mirror is required.
- A thin `Build OnePlus 13 Kernel` workflow exposes only OnePlus 13 options.
- The workflow resolves the KernelSU ref to a concrete commit SHA before building. KernelSU-Next's `setup.sh` checks out the *latest tag* when given no argument, and that tag lags the SUSFS patch set: `10_enable_susfs_for_ksu.patch` expects a `kernel/Kconfig` that only exists on `dev`, so a tag build leaves a `kernel/Kconfig.rej` with no corresponding fix patch and the build aborts. Upstream always passes an explicit ref, so this workflow does too.

## Features

- **KernelSU variants**: KernelSU-Next (`KSUN`) and original KernelSU (`KSU`)
- **SUSFS**: version auto-detected from the SUSFS branch (currently `v2.2.0` on `gki-android15-6.6`), with the upstream KSU-side patch/rej-fix logic
- **Nethunter support**: Bluetooth adapters (HCIBTUSB variants, BCM203X, BPA10X, BFUSB), SDR (AirSpy, HackRF), full CAN stack (VCAN, SLCAN, C_CAN, CC770, M_CAN, HI311X, MCP251X, 8DEV/EMS/ESD/GS/KVASER/PEAK USB), serial adapters (CH341, FTDI, PL2303)
- **Wireless modules**: External Wi-Fi/Bluetooth adapters built as loadable modules — Atheros (ath9k_htc, ath10k_usb, carl9170), Realtek (rtl8187, rtl8xxxu, rtw88 from lwfinger/rtw88), Ralink (rt2500usb, rt73usb, rt2800usb), Zydas (zd1211rw), Intersil (p54usb), MediaTek (mt7601u, mt76x0u, mt76x2u, mt7921u), and mac80211_hwsim. Ships as `kernel_modules_<MODEL>_<OS_VERSION>_<KERNEL_VER>.zip` with a `nethunter-wifi.sh` loader that resolves dependency order and swaps the platform Wi-Fi stack in one command.
- **Nethunter Wireless Firmware**: re-published unmodified from [nullptr-t-oss/Nethunter-Wireless-Firmware](https://github.com/nullptr-t-oss/Nethunter-Wireless-Firmware)
- **HMBIRD (Fengchi) scheduler** patches for the OnePlus 13 (SM8750)
- **Optimization patches**: memory, VFS, scheduler and network tweaks
- **Networking**: BBR, BBRv3, TTL target, IP_SET
- **Other**: NTSync, Unicode fix, Droidspaces, module intercept/overlay, vendor-module debloat
- **Compiler options**: pinned ZyC Clang 19 or manifest Clang, `O2`/`O3`, and `thin`/`full`/`none` LTO
- **Multi-version support**: all six OnePlus 13 kernel versions (6.6.{89,118} A16, 6.6.{66,30,89 CPH,56 CPH} A15)
- **Release modes**: artifact-only, prerelease, or stable release
- **ccache** acceleration with a release-backed cache

## Not included

- **No boot.img / vendor_boot / DLKM image builder.** The build produces a raw `Image`, an AnyKernel3 flashable ZIP, and a kernel modules ZIP only.
- **No other devices.** This repository targets the OnePlus 13 exclusively.

## Device variants

| Model | SoC | Kernel | OS | Manifest |
|-------|-----|--------|----|----|
| OP13-6.6.89 | SM8750 | 6.6.89 | A16 | oneplus_13_6.6.89_w.xml |
| OP13-6.6.118 | SM8750 | 6.6.118 | A16 | oneplus_13_6.6.118_w.xml |
| OP13-6.6.66 | SM8750 | 6.6.66 | A15 | oneplus_13_6.6.66_v.xml |
| OP13-6.6.30 | SM8750 | 6.6.30 | A15 | oneplus_13_6.6.30_v.xml |
| OP13-CPH-6.6.89 | SM8750 | 6.6.89 | A15 (global) | oneplus_13_global_6.6.89_v.xml |
| OP13-CPH-6.6.56 | SM8750 | 6.6.56 | A15 (global) | oneplus_13_global_6.6.56_v.xml |

All variants share the same SoC (Snapdragon 8 Elite / SM8750 / "sun"), Android version (android15), and manifest branch (`wild/sm8750`).

## Usage

1. Open the **Actions** tab and select **Build OnePlus 13 Kernel**.
2. Click **Run workflow** and choose options:

| Input | Description | Default |
|-------|-------------|---------|
| `kernel_version` | OnePlus 13 variant to build | `6.6.89 A16` |
| `ksu_variant` | `KSUN` or `KSU` | `KSUN` |
| `ksu_branch` | KernelSU branch/tag/commit (empty = `dev` for KSUN, `main` for KSU) | empty |
| `use_susfs` | Enable SUSFS | `true` |
| `susfs_branch` | SUSFS branch/commit (empty = `gki-android15-6.6`) | empty |
| `nethunter` | Enable Nethunter inline configs | `true` |
| `wireless_modules` | Build and package wireless kernel modules | `true` |
| `optimize_level` | `O2` or `O3` | `O2` |
| `lto` | `thin`, `full`, or `none` | `thin` |
| `compiler` | Pinned ZyC Clang 19 or manifest Clang | `zycromerz-19` |
| `use_opt_patches` | Apply optimization patches | `true` |
| `kernel_uname` | uname suffix | `OP-WILD` |
| `build_timestamp` | Custom uname timestamp (empty = current UTC) | empty |
| `clean_build` | Build without ccache restore | `false` |
| `release_type` | `none`, `prerelease`, or `release` | `none` |
| `debug` | Build modules and upload debug artifacts | `false` |

The `kernel_version` input supports `all`, which builds every variant in parallel.

3. Download artifacts from the run's **Artifacts**, or from the release when `release_type` is `prerelease` or `release`:
   - `AK3_<MODEL>_<OS>_<KERNEL>_<KSU>_<VER>.zip` — AnyKernel3 flashable ZIP
   - `kernel_modules_<MODEL>_<OS>_<KERNEL>.zip` — wireless/CAN driver modules (when `wireless_modules` is enabled)
   - `Nethunter-Wireless-Firmware-<VER>.zip` — firmware archive (when `wireless_modules` is enabled)
   - `Image_<MODEL>_<KERNEL>` — raw kernel Image

### Installation on device

1. Flash the AnyKernel3 ZIP in a custom recovery, or via the KernelSU/APatch/other flasher.
2. Reboot and install the matching manager app:
   - KernelSU-Next: https://github.com/KernelSU-Next/KernelSU-Next/releases
   - KernelSU: https://github.com/tiann/KernelSU/releases

### Using wireless modules

Kernel modules are built with `CONFIG_MODVERSIONS=y`, so a module ZIP is valid **only** for the exact matching kernel build. Modules from one release will not load on another, even if both use the same base kernel version.

The ZIP ships a loader script, `nethunter-wifi.sh`, plus a flattened `modules.dep`. Extract the ZIP on the device and run it as root:

```bash
./nethunter-wifi.sh load            # default: ath9k_htc (AR9271)
./nethunter-wifi.sh load mt76x2u    # or any driver in the pack
./nethunter-wifi.sh list            # every driver available
./nethunter-wifi.sh status          # what is resident, plus PHY and regdomain
./nethunter-wifi.sh monitor "" 6    # monitor mode on channel 6
./nethunter-wifi.sh managed         # back out of monitor mode
./nethunter-wifi.sh restore         # unload and put the internal Wi-Fi back
```

`load` resolves the dependency chain from `modules.dep`, unloads the platform Wi-Fi stack when the requested driver needs `mac80211`, and points the firmware loader at whichever directory holds the Nethunter firmware blobs. Extract the firmware ZIP first, or `ath9k_htc` will bind to the adapter and then fail to fetch `ath9k_htc/htc_9271-1.4.0.fw`.

`restore` reloads the platform modules cleanly, but Android's `WifiService` generally stays latched in a failed state afterwards — in testing the internal Wi-Fi did **not** come back without a reboot. Treat a reboot as the normal way to end a session with an external adapter.

#### Verified hardware

Atheros **AR9271** (`040d:3801`, "VIA USB2.0 WLAN"), on a OnePlus 13 running the 6.6.118 A16 build, over USB OTG:

```
usb 1-1: ath9k_htc: Firmware ath9k_htc/htc_9271-1.4.0.fw requested
usb 1-1: ath9k_htc: Transferred FW: ath9k_htc/htc_9271-1.4.0.fw, size: 51008
ath9k_htc 1-1:1.0: ath9k_htc: HTC initialized with 33 credits
ath9k_htc 1-1:1.0: ath9k_htc: FW Version: 1.4
usbcore: registered new interface driver ath9k_htc
```

`phy0` registered, `wlan0` created, and `iw phy` reports IBSS / managed / **AP** / AP-VLAN / **monitor** / P2P-client / P2P-GO. Monitor mode confirmed end to end via `./nethunter-wifi.sh monitor "" 6`: the interface came up as `link/ieee802.11/radiotap` on 2437 MHz at 20 dBm, and `tcpdump -i wlan0` captured live 802.11 control and data frames (514 frames received by the filter in 6 seconds, 0 dropped by the kernel). No unknown-symbol or version-disagreement errors anywhere in the chain.

To load at every boot (KernelSU/Magisk), `./nethunter-wifi.sh install` copies the pack to `/data/adb/nethunter-wifi` and adds a `service.d` hook. This costs the internal Wi-Fi on **every** boot; undo it with `./nethunter-wifi.sh uninstall`.

#### Regulatory database

`cfg80211` looks for `regulatory.db` and Android does not ship one, so the log shows:

```
cfg80211: failed to load regulatory.db
```

The radio then falls back to the world regulatory domain (`country 00`). Verified consequences on an AR9271: 20 dBm cap on every channel, channels 12–14 flagged `no IR` (no initiating radiation — injection and beaconing are blocked there), 5 GHz entirely `PASSIVE-SCAN`, and `iw reg set US` accepted but silently ignored. Channels 1–11 are fully usable, including monitor mode and injection.

To lift it, install `regulatory.db` and `regulatory.db.p7s` from [wireless-regdb](https://git.kernel.org/pub/scm/linux/kernel/git/sforshee/wireless-regdb.git) into a directory `ueventd` searches. Setting `/sys/module/firmware_class/parameters/path` is **not** sufficient — the kernel's direct load from `/data` fails with `-2` under SELinux (`shell_data_file` is not readable by the firmware loader) and only succeeds because ueventd's usermode helper then finds the blob on its own search path. That list is fixed in `/system/etc/ueventd.rc`:

```
/etc/firmware/  /odm/firmware/  /data/vendor/firmware/update/  /vendor/firmware/
/firmware/image/  /vendor/firmware_mnt/image/qca6490/  /data/oplus/fw_update/
/mnt/vendor/persist/copy/  /mnt/vendor/persist/  /odm/etc/wifi/  /vendor/firmware_mnt/image/
```

A KernelSU/Magisk module that overlays `system/etc/firmware/` is the practical route, which is also how the Nethunter firmware ZIP delivers the ath9k blobs.

#### Why these drivers are not built into the Image

`ath9k_htc` — the AR9271 driver — is `depends on USB && MAC80211` in Kconfig, and Kconfig will not let a built-in driver depend on a modular provider. `CONFIG_ATH9K_HTC=y` therefore forces `CONFIG_MAC80211=y`, `CONFIG_CFG80211=y` and `CONFIG_RFKILL=y`.

That is not survivable here. OnePlus builds its own `cfg80211` in a separate tree with different symbol CRCs — the bundled `mac80211` already refuses to load against the platform one (`mac80211: disagrees about version of symbol wiphy_new_nm`). With `cfg80211` compiled into the Image, `/vendor/lib/modules/cfg80211.ko` can no longer load at all, so `qca_cld3_peach_v2` fails its CRC check on every boot and the internal Wi-Fi is permanently dead. Keeping the drivers modular confines that cost to the moment you actually run the loader, and a reboot undoes it.

The same applies to the manual procedure: both `cfg80211.ko` **and** `mac80211.ko` must come from the ZIP. Verified on a OnePlus 13 running the 6.6.118 A16 build:

```bash
# Turn Wi-Fi off in Settings first, then unload the stock stack
rmmod qca_cld3_peach_v2
rmmod cfg80211

# Load bundled stack
insmod /path/to/cfg80211.ko
insmod /path/to/mac80211.ko

# Load driver (example: ath9k_htc)
insmod /path/to/ath.ko
insmod /path/to/ath9k_hw.ko
insmod /path/to/ath9k_common.ko
insmod /path/to/ath9k_htc.ko
```

Displacing the platform `cfg80211` costs the internal Wi-Fi for the rest of the boot. Reversing the steps reloads the kernel modules fine, but Android's `WifiService` stays latched in a failed state — reboot to get internal Wi-Fi back.

See EmberHeart's `docs/drivers.md` for the full procedure and per-driver dependency chains.

Extract the firmware ZIP to `/vendor/firmware/` or the location your distribution expects.

## Repository layout

```
.github/
  workflows/build-oneplus13-kernel.yml   # OnePlus 13 multi-version matrix workflow
  actions/
    build-kernel/                         # vendored upstream build pipeline (pinned) + Nethunter extensions
    kernel-source-sync/                   # vendored source/toolchain sync (cache source retargeted)
    cache/{restore,save}/                 # vendored release-backed ccache helpers
configs/
  OP13-6.6.{89,118,66,30}.json            # OnePlus 13 device configs (A16, A15)
  OP13-CPH-6.6.{89,56}.json               # OnePlus 13 global device configs (A15)
manifests/
  a16/oneplus_13_6.6.{89,118}_w.xml       # pinned OnePlus 13 manifests (OxygenOS 16)
  a15/oneplus_13_6.6.{66,30}_v.xml        # pinned OnePlus 13 manifests (OxygenOS 15)
  a15/oneplus_13_global_6.6.{89,56}_v.xml # pinned OnePlus 13 global manifests (OxygenOS 15)
```

## Credits

- Build pipeline, patches, manifest and toolchain cache: [WildKernels/OnePlus_KernelSU_SUSFS](https://github.com/WildKernels/OnePlus_KernelSU_SUSFS) and [WildKernels/kernel_patches](https://github.com/WildKernels/kernel_patches)
- Nethunter wireless drivers, module packaging, and firmware passthrough adapted from [nullptr-t-oss/EmberHeart_OnePlus11](https://github.com/nullptr-t-oss/EmberHeart_OnePlus11)
- Workflow controls adapted from EmberHeart: selectable compiler source, O2/O3, LTO mode, KSU/SUSFS refs, optional patch sets, uname/timestamp, clean build, debug modules, and stable/prerelease publishing
- Out-of-tree rtw88 drivers: [lwfinger/rtw88](https://github.com/lwfinger/rtw88)
- Nethunter Wireless Firmware: [nullptr-t-oss/Nethunter-Wireless-Firmware](https://github.com/nullptr-t-oss/Nethunter-Wireless-Firmware)
- [KernelSU](https://github.com/tiann/KernelSU), [KernelSU-Next](https://github.com/KernelSU-Next/KernelSU-Next), [SUSFS](https://gitlab.com/simonpunk/susfs4ksu), [AnyKernel3](https://github.com/osm0sis/AnyKernel3), [OnePlusOSS](https://github.com/OnePlusOSS)

## Licensing

Workflow configuration is provided as-is. Kernel source and patches retain their upstream licenses (Linux kernel GPL-2.0; KernelSU GPL-3.0; SUSFS GPL-2.0).

## Security

Never commit personal access tokens. Releases use the automatically provided `GITHUB_TOKEN`. If you pasted a PAT anywhere while setting this up, revoke it at https://github.com/settings/tokens.
