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
- **Wireless modules**: External Wi-Fi/Bluetooth adapters built as loadable modules — Atheros (ath9k_htc, ath10k_usb, carl9170), Realtek (rtl8187, rtl8xxxu, rtw88 from lwfinger/rtw88), Ralink (rt2500usb, rt73usb, rt2800usb), Zydas (zd1211rw), Intersil (p54usb), MediaTek (mt7601u, mt76x0u, mt76x2u, mt7921u), and mac80211_hwsim. Ships as `kernel_modules_<MODEL>_<OS_VERSION>_<KERNEL_VER>.zip`.
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

OnePlus's stock mac80211 stack lacks symbols these drivers need (`__ieee80211_create_tpt_led_trigger`, etc.). You must unload the stock stack and load the bundled `cfg80211.ko`/`mac80211.ko` before the driver module. Example:

```bash
# Unload stock stack
rmmod wlan
rmmod cfg80211

# Load bundled stack
insmod /path/to/cfg80211.ko
insmod /path/to/mac80211.ko

# Load driver (example: ath9k_htc)
insmod /path/to/ath9k_hw.ko
insmod /path/to/ath9k_common.ko
insmod /path/to/ath9k_htc.ko
```

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
