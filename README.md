# OnePlus13-KernelBuilder

A GitHub Actions CI/CD pipeline that builds a custom, feature-packed kernel for the **OnePlus 13** (Qualcomm SM8750, codename `sun`). For each variant it produces:

- A flashable **AnyKernel3 ZIP** (`AK3_*.zip`)
- A raw ARM64 **`Image`**
- A **wireless/CAN modules pack** (`kernel_modules_*.zip`) plus a **firmware passthrough ZIP**

The pipeline is a single-device fork of [WildKernels/OnePlus_KernelSU_SUSFS](https://github.com/WildKernels/OnePlus_KernelSU_SUSFS), pinned to a specific commit and reduced to the OnePlus 13 `sun` platform only. It builds from a pinned `repo`-style manifest, applies a curated series of patches (KernelSU/SUSFS, NetHunter, scheduler and power tweaks, battery optimizations), and packages the results without constructing any boot/vendor/DLKM images.

> **This repository intentionally produces no `boot.img`, `vendor_boot.img`, `vendor_dlkm.img`, or `system_dlkm.img`.**

---

## Features

- **Root**: KernelSU-Next (KSUN, `dev` branch default) or KernelSU (KSU, `main` branch default), resolved to a concrete commit SHA before building.
- **SUSFS** (optional, default on) with the simonpunk patch set and the fork-specific kernel compat fixes.
- **NetHunter**: inline configs (Bluetooth, SDR/AirSpy/HackRF, CAN bus, USB-serial) **plus** external Wi-Fi adapter drivers (`ath9k_htc`/AR9271, `ath10k_usb`, `carl9170`, `rtl8187`, `rtl8xxxu`, `rtw88`, `rt2x00`, `zd1211rw`, `p54`, `mt76`) shipped as loadable modules.
- **`feat/perf-stack` kernel series** (via the Hipuu common-kernel fork): MGLRU rework + workingset/refault fixes, `af_unix` GC rewrite (Tarjan SCC), BPF optimizations, `f2fs` GC/sparse-read/deadlock fixes, THP `__GFP_THISNODE` reclaim fix, cpufreq/sched `NEED_UPDATE_LIMITS` fixes, workqueue/videobuf2 tweaks, `zsmalloc` enabled + zram tracking, UKSM + HMBIRD hardening, migration vendor hooks + ZTE symbols.
- **Perf-stack extras**: **ADIOS** I/O scheduler, **zram lz4/zstd backports**, and **Re-Kernel** freeze-notification LKM, each pinned by commit.
- **Battery optimizations**: see [Battery & power tweaks](#battery--power-tweaks).
- **Networking**: BBR / BBRv3, TTL target, IP_SET & IPv6 NAT, qdisc schedulers (FQ/CAKE/PIE), NTSync, TMPFS xattr/ACL.
- **Feature toggles**: `use_susfs`, `nethunter`, `wireless_modules`, `use_opt_patches`, `hmbird`/`ds`/`bbg`/`ttl`/`ip_set`/`ntsync` (per-config), plus per-run `lto` (none/thin/full) and `optimize_level` (O2/O3).
- **DDK / Bazel-Kleaf builds** (6.6.118 A16): builds `qca_cld3_<chipset>.ko` against the vendor kernel, with an optional **monitor-mode frame-injection patch** for the internal Wi-Fi; stripped `--strip-debug`, `--jobs`/heap computed for the host, and a Bazel disk cache.

---

## Supported variants

All six share the same SoC (`SM8750`), Android generation (`android15`), and manifest branch (`wild/sm8750`); they differ in kernel version and OxygenOS generation.

| Config | Kernel | OS | Manifest |
|---|---|---|---|
| `configs/OP13-6.6.89.json` | 6.6.89 | A16 | `manifests/a16/oneplus_13_6.6.89_w.xml` |
| `configs/OP13-6.6.118.json` | 6.6.118 | A16 | `manifests/a16/oneplus_13_6.6.118_w.xml` |
| `configs/OP13-6.6.66.json` | 6.6.66 | A15 | `manifests/a15/oneplus_13_6.6.66_v.xml` |
| `configs/OP13-6.6.30.json` | 6.6.30 | A15 | `manifests/a15/oneplus_13_6.6.30_v.xml` |
| `configs/OP13-CPH-6.6.89.json` | 6.6.89 | A15 global | `manifests/a15/oneplus_13_global_6.6.89_v.xml` |
| `configs/OP13-CPH-6.6.56.json` | 6.6.56 | A15 global | `manifests/a15/oneplus_13_global_6.6.56_v.xml` |

---

## Running a build

Builds are only triggered through GitHub Actions (`workflow_dispatch`). There is no local build script.

```bash
# Default build (6.6.89 A16, KSUN + SUSFS + NetHunter + wireless modules)
gh workflow run "Build OnePlus 13 Kernel" -R Hipuu/OnePlus13-KernelBuilder

# A single non-default variant
gh workflow run "Build OnePlus 13 Kernel" -R Hipuu/OnePlus13-KernelBuilder \
  -f kernel_version="6.6.118 A16"

# All six variants in parallel
gh workflow run "Build OnePlus 13 Kernel" -R Hipuu/OnePlus13-KernelBuilder \
  -f kernel_version=all

# Self-hosted runner pool (requires labels: self-hosted, linux, X64)
gh workflow run "Build OnePlus 13 Kernel" -R Hipuu/OnePlus13-KernelBuilder \
  -f runner=self-hosted
```

### Key dispatch inputs

| Input | Options / default | Notes |
|---|---|---|
| `kernel_version` | one of the six, or `all` | The build matrix. |
| `ksu_variant` | `KSUN` (default), `KSU` | Root implementation. |
| `ksu_branch` | free text, empty = default | Empty uses `main` for KSU, `dev` for KSUN (falling back to a pinned compatible commit when recorded). |
| `use_susfs` | `true` (default) | SUSFS feature set. |
| `susfs_branch` | free text, empty = auto | Empty selects the GKI branch / pinned compatible commit for the tree. |
| `nethunter` / `wireless_modules` | `true` (default) | Inline configs / external driver modules. |
| `ddk` / `ddk_injection` | `true` (default) | Bazel-Kleaf build + qcacld monitor injection (6.6.118 A16 only; ignored elsewhere). |
| `optimize_level` | `O2` (default), `O3` | Compiler optimization. |
| `lto` | `thin` (default), `full`, `none` | Link-time optimization. `thin` enables the persistent ThinLTO cache. |
| `compiler` | `zycromerz-19` (default), `manifest` | Toolchain source. |
| `release_type` | `none` (default), `prerelease`, `release` | Publish the build to a GitHub release. |
| `runner` | `github-hosted` (default), `self-hosted` | Runner pool. |

---

## Self-hosted runner requirements

The `self-hosted` pool is required for the DDK/Bazel variant (`feat/perf-stack`): the fork kernel graph exceeds the 16 GB RAM ceiling of GitHub-hosted runners during Bazel's loading phase. Register a runner with labels `self-hosted`, `linux`, `X64`.

- **RAM**: ≥ 32 GB (64 GB recommended for `all` matrix runs).
- **Disk**: ≥ 250 GB free per concurrent job. The workspace is wiped at the start of every `build` job; a variant-aware "Check free disk space" step hard-fails below 50 GiB free (150 GiB for the Bazel DDK variant, whose output base alone can exceed a 100 GB disk).
- **OS**: Ubuntu 22.04+; the workflow installs its own dependencies via `apt-get`.
- **No sudo required**: the `repo` binary and all temp files live under `$RUNNER_TEMP`; the workspace cleanup also prunes stale `~/.cache/bazel` output bases.
- **Concurrency**: matrix jobs serialize via a top-level `concurrency:` group, since they share the physical host (`/dev/shm`, disk I/O, RAM). GitHub-hosted is unaffected (each job gets its own VM).

Swap is only configured for GitHub-hosted runners (16 GB, via `pierotofy/set-swap-space`); self-hosted hosts are expected to have sufficient physical RAM instead.

---

## Build caches

Caches are stored in **GitHub Releases** via the custom `cache/restore` and `cache/save` composite actions, not the `actions/cache` service.

| Cache | Bucket | Gating |
|---|---|---|
| ccache | `ccache-cache` | make-based, non-clean builds |
| Bazel disk cache | `bazel-disk-cache` | DDK (`OP_DDK`) builds |
| **ThinLTO cache** | `lto-cache` | make-based + `lto=thin`, non-clean builds |

**ThinLTO cache** (`ld-cache`): on make-based thin-LTO builds, `ld-wrapper` invokes `ld.lld "$@" --thinlto-cache-dir=… --thinlto-jobs=$((nproc/2))`, so unchanged modules skip re-importing/re-optimizing between runs. The cache dir (`ldcache_<KERNEL_FULL_VER>` under the workspace) is restored and saved with both a primary key and `restore_keys` fallbacks; the saved side uses `delete_after_upload` so it does not bloat the disk. DDK/Kleaf builds never use `ld-wrapper` and skip this cache.

Version keys include the KernelSU variant, model, OS version, full kernel version, and Clang fingerprint, so caches never cross-link across toolchains or variants.

---

## Artifacts

- `AK3_<MODEL>_<OS>_<KERNEL>_<KSU>_<VER>[_SuSFS_<ver>].zip` — flashable AnyKernel3 package.
- `Image` — raw ARM64 kernel image.
- `kernel_modules_<MODEL>_<OS>_<KERNEL>.zip` — external Wi-Fi/CAN modules, a flattened `modules.dep`, and the `nethunter-wifi.sh` loader.
- `Nethunter-Wireless-Firmware-<VER>.zip` — re-published unmodified from `nullptr-t-oss/Nethunter-Wireless-Firmware`.
- `qca_cld3_peach_v2.ko` (DDK builds) — the vendor Wi-Fi module as a standalone artifact.
- Debug artifacts when `debug=true` (build/install logs, `vmlinux`, `Module.symvers`, modules tree).

Modules are built with `CONFIG_MODVERSIONS=y`, so a modules ZIP loads **only** on the exact kernel build it shipped with.

### Using wireless modules on-device

```bash
# from the extracted module pack, as root
./nethunter-wifi.sh            # interactive menu
./nethunter-wifi.sh load       # ath9k_htc by default
./nethunter-wifi.sh status
./nethunter-wifi.sh restore
./nethunter-wifi.sh conmode monitor 6   # internal Wi-Fi -> monitor mode
./nethunter-wifi.sh conmode sta         # back to normal
```

The loader displaces the platform Wi-Fi stack only when a loaded driver actually needs `mac80211`; a reboot (or `restore`) brings the internal Wi-Fi back. See the scroll header inside `nethunter-wifi.sh` for the full rationale.

---

## Battery & power tweaks

A dedicated `Apply battery optimization patches` step (`.github/actions/build-kernel/files/battery/`) applies an idempotent, `--forward`-safe series on every base:

- **Wakelock entropy**: a **global 500 ms timeout** on newly-created wakelocks (`kernel/power/wakelock.c`), so stray locks like `tx_swr_ctrl` cannot pin the CPU awake.
- **s2idle wakeups**: only wake once from s2idle (`pm_system_wakeup`).
- **Freeze timeout**: reduced to 1 s for Android, and made non-tunable from userspace.
- **ext4/f2fs commit windows**: larger default commit age / `min_fsync_blocks` so writes batch and the CPU stays idle longer.
- **`alarmtimer` wakeup**: minimized wake timeout to the nearest timer expiration.
- **PCI PME checks**: `PME_TIMEOUT` 1000 → 4000 ms.
- **Log spam**: silence `devkmsg` and IRQ-affinity log spam.
- **hrtimer**: avoid pointless reprogramming when the hrtimer tick is already running (upstream stable commit).
- **Vendor tasktracker (A1)**: gate the `oplus_bsp_schedinfo` periodic hrtimer on `tasktrack_enable` so it cannot fire ~7.5×/s when disabled. The corresponding patch targets the vendor tree and is applied from `vendor/oplus/kernel` (the parent of the `cpu/sched/...` path).

> These are battery/performance trade-offs: the global wakelock timeout and the reduced freeze timeout change how aggressively the device can suspend. If any breaks a vendor feature you rely on, disable just that patch (they are independent files).

---

## Repository layout

```
.github/
  workflows/build-oneplus13-kernel.yml   # Top-level workflow (workflow_dispatch)
  compatible-commits.json                # Pinned, verified SUSFS/KSUN commits
  actions/
    build-kernel/action.yml              # The actual build (patches, make/Kleaf, packaging)
    build-kernel/files/                  # battery/*.patch, ddk/*, nethunter-wifi.sh, apply-susfs-main-patch.sh
    kernel-source-sync/action.yml        # Downloads pinned sources/toolchains from the manifest
    cache/restore|save/action.yml        # Release-backed ccache / Bazel / ThinLTO caches
configs/                                 # JSON device configs (one per variant)
manifests/                               # XML repo manifests (one per variant)
README.md                                # This file
```

---

## Development

### Local validation

```bash
SKIP_NETWORK=1 bash validate_workflow.sh
```

(omit `SKIP_NETWORK` to also check pinned toolchain-cache assets are reachable). The validator checks YAML/JSON/XML parseability, DDK **and** battery patch hunk counts, workflow/action structure, config↔manifest alignment, full-SHA pins, absence of boot-image construction, and POSIX/LF conformance of the on-device loader.

### Debug helper

```bash
./debug_workflow.sh [status|logs|failed|watch|rerun|artifacts|download] [run_id]
```

### Notes

- Comments, docs, and commit messages are in English.
- `.sh` files keep **LF** line endings (enforced by `.gitattributes`); the on-device loader is POSIX `sh`.
- Do not loosen SHA pinning on manifests or the pinned perf-stack commits — rebuilds depend on reproducibility.
- This repo does **not** build boot images by design; do not add `mkbootimg`/`avbtool`/`boot.img`/DLKM construction commands.