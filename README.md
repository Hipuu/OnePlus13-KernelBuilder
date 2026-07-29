# OnePlus 13 Kernel Builder

A GitHub Actions workflow that builds a custom **OnePlus 13** kernel with KernelSU / KernelSU-Next and SUSFS. It reuses the proven, actively maintained build pipeline from [WildKernels/OnePlus_KernelSU_SUSFS](https://github.com/WildKernels/OnePlus_KernelSU_SUSFS), pinned to a specific commit and specialized to the OnePlus 13 only.

## Why it is built this way

The OnePlus 13 (SM8750 / "sun") kernel is built with WildKernels' manifest fork, pinned source/toolchain revisions, and its maintained patch pipeline. Rather than re-implement ~2400 lines of patch ordering and risk drift, this repository vendors the upstream build logic from commit `bfe12144`, pins `WildKernels/kernel_patches` to commit `24865a0`, and changes only what is required to run it standalone for one device:

- The composite `build-kernel` action's internal sub-action references are pointed at this repository's copies.
- The source-sync step downloads the pinned Clang, kernel build-tools and AnyKernel3 archives from WildKernels' public `toolchain-cache` release, so no per-repository toolchain mirror is required.
- A thin `Build OnePlus 13 Kernel` workflow exposes only OnePlus 13 options.
- The workflow resolves the KernelSU ref to a concrete commit SHA before building. KernelSU-Next's `setup.sh` checks out the *latest tag* when given no argument, and that tag lags the SUSFS patch set: `10_enable_susfs_for_ksu.patch` expects a `kernel/Kconfig` that only exists on `dev`, so a tag build leaves a `kernel/Kconfig.rej` with no corresponding fix patch and the build aborts. Upstream always passes an explicit ref, so this workflow does too.

## Features

- **KernelSU variants**: KernelSU-Next (`KSUN`) and original KernelSU (`KSU`)
- **SUSFS**: version auto-detected from the SUSFS branch (currently `v2.2.0` on `gki-android15-6.6`), with the upstream KSU-side patch/rej-fix logic
- **HMBIRD (Fengchi) scheduler** patches for the OnePlus 13 (SM8750)
- **Optimization patches**: memory, VFS, scheduler and network tweaks
- **Networking**: BBR, BBRv3, TTL target, IP_SET
- **Other**: NTSync, Unicode fix, Droidspaces, module intercept/overlay, vendor-module debloat
- **Compiler options**: pinned ZyC Clang 19 or manifest Clang, `O2`/`O3`, and `thin`/`full`/`none` LTO
- **Release modes**: artifact-only, prerelease, or stable release
- **ccache** acceleration with a release-backed cache

## Not included

- **No boot.img / vendor_boot / DLKM image builder.** The build produces a raw `Image` and an AnyKernel3 flashable ZIP only.
- **No other devices.** This repository targets the OnePlus 13 exclusively.

## Device

| Field | Value |
|-------|-------|
| Model | OnePlus 13 (`OP13-6.6.89`) |
| SoC | Snapdragon 8 Elite (SM8750 / `sun`) |
| Android | android15 |
| Kernel | 6.6 |
| OS | OxygenOS 16 (`A16`) |
| Manifest branch | `wild/sm8750` (WildKernels fork) |
| Manifest file | `oneplus_13_6.6.89_w.xml` (vendored under `manifests/a16/`) |

## Usage

1. Open the **Actions** tab and select **Build OnePlus 13 Kernel**.
2. Click **Run workflow** and choose options:

| Input | Description | Default |
|-------|-------------|---------|
| `ksu_variant` | `KSUN` or `KSU` | `KSUN` |
| `ksu_branch` | KernelSU branch/tag/commit (empty = `dev` for KSUN, `main` for KSU) | empty |
| `use_susfs` | Enable SUSFS | `true` |
| `susfs_branch` | SUSFS branch/commit (empty = `gki-android15-6.6`) | empty |
| `optimize_level` | `O2` or `O3` | `O2` |
| `lto` | `thin`, `full`, or `none` | `thin` |
| `compiler` | Pinned ZyC Clang 19 or manifest Clang | `zycromerz-19` |
| `use_opt_patches` | Apply optimization patches | `true` |
| `kernel_uname` | uname suffix | `OP-WILD` |
| `build_timestamp` | Custom uname timestamp (empty = current UTC) | empty |
| `clean_build` | Build without ccache restore | `false` |
| `release_type` | `none`, `prerelease`, or `release` | `none` |
| `debug` | Build modules and upload debug artifacts | `false` |

3. Download the AnyKernel3 ZIP (and raw `Image`) from the run's **Artifacts**, or from the release when `release_type` is `prerelease` or `release`.

### Installation on device

1. Flash the AnyKernel3 ZIP in a custom recovery, or via the KernelSU/APatch/other flasher.
2. Reboot and install the matching manager app:
   - KernelSU-Next: https://github.com/KernelSU-Next/KernelSU-Next/releases
   - KernelSU: https://github.com/tiann/KernelSU/releases

## Repository layout

```
.github/
  workflows/build-oneplus13-kernel.yml   # OnePlus 13-only driver workflow
  actions/
    build-kernel/                         # vendored upstream build pipeline (pinned)
    kernel-source-sync/                   # vendored source/toolchain sync (cache source retargeted)
    cache/{restore,save}/                 # vendored release-backed ccache helpers
configs/OP13-6.6.89.json                  # OnePlus 13 device config
manifests/a16/oneplus_13_6.6.89_w.xml     # pinned OnePlus 13 manifest
```

## Credits

- Build pipeline, patches, manifest and toolchain cache: [WildKernels/OnePlus_KernelSU_SUSFS](https://github.com/WildKernels/OnePlus_KernelSU_SUSFS) and [WildKernels/kernel_patches](https://github.com/WildKernels/kernel_patches)
- Workflow controls adapted from [nullptr-t-oss/EmberHeart_OnePlus11](https://github.com/nullptr-t-oss/EmberHeart_OnePlus11): selectable compiler source, O2/O3, LTO mode, KSU/SUSFS refs, optional patch sets, uname/timestamp, clean build, debug modules, and stable/prerelease publishing. EmberHeart's OnePlus 11 module, boot/vendor_boot, and DLKM image targets are intentionally excluded.
- [KernelSU](https://github.com/tiann/KernelSU), [KernelSU-Next](https://github.com/KernelSU-Next/KernelSU-Next), [SUSFS](https://gitlab.com/simonpunk/susfs4ksu), [AnyKernel3](https://github.com/osm0sis/AnyKernel3), [OnePlusOSS](https://github.com/OnePlusOSS)

## Licensing

Workflow configuration is provided as-is. Kernel source and patches retain their upstream licenses (Linux kernel GPL-2.0; KernelSU GPL-3.0; SUSFS GPL-2.0).

## Security

Never commit personal access tokens. Releases use the automatically provided `GITHUB_TOKEN`. If you pasted a PAT anywhere while setting this up, revoke it at https://github.com/settings/tokens.
