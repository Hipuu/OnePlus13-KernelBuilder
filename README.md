# OnePlus13-KernelBuilder

**Custom GKI kernel for the OnePlus 13 (`sun` / SM8750) — KernelSU-Next + SUSFS + NetHunter, with working internal-radio monitor-mode injection.**

[![Build](https://img.shields.io/badge/Build-GitHub_Actions-blue?style=flat-square)](../../actions)
[![KernelSU-Next](https://img.shields.io/badge/KernelSU--Next-supported-green?style=flat-square)](https://kernelsu-next.github.io/webpage/)
[![SUSFS](https://img.shields.io/badge/SUSFS-integrated-orange?style=flat-square)](https://gitlab.com/simonpunk/susfs4ksu)
[![Injection](https://img.shields.io/badge/monitor_injection-verified-brightgreen?style=flat-square)](#-monitor-mode-injection)

A GitHub Actions CI/CD pipeline that builds a custom, feature-packed kernel for the **OnePlus 13** (Qualcomm SM8750, codename `sun`). Each variant produces:

- A flashable **AnyKernel3 ZIP** (`AK3_*.zip`)
- A raw ARM64 **`Image`**
- A **wireless/CAN modules pack** (`kernel_modules_*.zip`) plus a **firmware passthrough ZIP**

> **This repository intentionally produces no `boot.img`, `vendor_boot.img`, `vendor_dlkm.img`, or `system_dlkm.img`.**

---

> [!CAUTION]
> ## Your warranty is no longer valid!
>
> I am not responsible for bricked devices, dead SD cards, thermonuclear war, or the current economic crisis. Please do some research if you have any concerns about features included in this kernel before flashing it! **YOU** are choosing to make these modifications, and if you point your finger at me for messing up your device, I will laugh at you.

---

> [!TIP]
> ## Installation
>
> 1. Flash the **AK3 ZIP** with [KernelFlasher](https://github.com/fatalcoder524/KernelFlasher/releases/latest) or TWRP.
> 2. Reboot. That is the whole kernel install — the AnyKernel3 package carries `Image` and patches the boot image in place.
> 3. *(Optional, NetHunter)* Unzip `kernel_modules_*.zip` to internal storage and load a driver with `nethunter-wifi.sh`. See [Wireless modules](#wireless-modules).
>
> **Verify the flash** with `/proc/version`, **not** `uname -r` — SUSFS rewrites the `uname` release string, so `uname -r` reports a plausible stock value that matches no partition on the device. It is not a sign the flash failed.

---

> [!NOTE]
> ## Features
>
> - 🔐 **Root** — KernelSU-Next (default) or KernelSU, resolved to a concrete commit SHA before building.
> - 🥷 **SUSFS** — root hiding (path/mount/kstat spoofing), optional, default on.
> - 🛡️ **BBG (Baseband Guard)** — LSM-based protection for critical device partitions.
> - 🐉 **NetHunter** — inline configs (Bluetooth, SDR/AirSpy/HackRF, CAN, USB-serial) **plus** external Wi-Fi drivers (`ath9k_htc`/AR9271, `ath10k_usb`, `carl9170`, `rtl8187`, `rtl8xxxu`, `rtw88`, `rt2x00`, `zd1211rw`, `p54`, `mt76`) as loadable modules.
> - 📡 **Monitor-mode injection** — the internal Qualcomm radio can transmit arbitrary 802.11 management frames. See [below](#-monitor-mode-injection).
> - ⚡ **`feat/perf-stack`** — MGLRU rework, `af_unix` GC rewrite, BPF opts, f2fs fixes, THP reclaim fix, cpufreq/sched `NEED_UPDATE_LIMITS`, zsmalloc + zram, UKSM, HMBIRD.
> - 🧩 **Extras** — **ADIOS** I/O scheduler, **zram lz4/zstd backports**, **Re-Kernel** freeze-notification LKM.
> - 🔋 **Battery** — an always-on patch series plus opt-in `battery_save` / `audit_off` knobs. See [Battery](#-battery--power-tweaks).
> - 🌐 **Networking** — BBR / BBRv3, TTL target, IP_SET & IPv6 NAT, FQ/CAKE/PIE, NTSync, TMPFS xattr/ACL.
> - 🖥️ **Droidspaces** — SYSVIPC / PID_NS / POSIX_MQUEUE for portable Linux containers.
> - 🚀 **Oryon tuning** — `-mcpu=oryon-1`, ThinLTO with a persistent cache.

---

> [!IMPORTANT]
> ## Monitor-mode injection
>
> The DDK build patches the vendor Wi-Fi driver (`qcacld-3.0` → `qca_cld3_<chipset>.ko`) so the **internal** radio can inject management frames — deauth, probe requests, and the rest — while in monitor mode. Toggle with `ddk_injection`.
>
> **Verified on-device** (6.6.142 A16, OnePlus 13):
>
> - Injected deauths measurably disconnect a station — **24–26 of 40** samples on an independent receiver, where the same test before the TX-power fix produced **0**.
> - **Stock wifite** runs end-to-end: 390 deauths on air, 62 EAPOL captured, and **2 complete crackable WPA 4-way handshakes** (`aircrack-ng`: `WPA (1 handshake)`).
>
> ### How it works
>
> Firmware drops management TX on a plain monitor vdev, so the patch creates a hidden **ghost STA vdev** via direct WMI (`VDEV_CREATE`/`VDEV_START`/`PEER_CREATE`) and routes injected frames out on *that* vdev, bypassing `wlan_mgmt_txrx` entirely via `wmi_mgmt_unified_cmd_send()`. Each buffer is tracked in a 32-slot table keyed by a 16-bit token — the only lane firmware echoes back, since `desc_id` is `uint16_t`.
>
> ### The traps (each cost real debugging time)
>
> | Trap | Symptom | Fix |
> |---|---|---|
> | Ghost vdev started with `maxregpower = 0` | Firmware reports `status 0`, frames appear on the phone's *own* monitor iface, **nothing is radiated** | Set `maxregpower`/`maxpower` |
> | Deauth sent with our own SA | Stations ignore it | Spoof `addr2` = the AP's BSSID |
> | `PEER_CREATE` under load | Firmware hangs ~135 ms later | Skip peer create while `inflight` is high |
> | Drop-and-continue at the TX cap | A fast injector still hangs firmware | `netif_tx_stop_all_queues()` + wake on drain |
> | Monitor netdev gets no `cfg80211` channel | `airodump` blocks on `CH 0`, captures ~2% | Synthesise `cfg80211_ch_switch_notify()` |
>
> > [!WARNING]
> > Monitor-mode sessions can wedge the firmware and leave Wi-Fi dead. **Reboot** to recover — an in-place mode switch has a documented panic class.

---

> [!TIP]
> ## Quick Start
>
> Builds run only through GitHub Actions (`workflow_dispatch`); there is no local build script.
>
> ```bash
> # Default (6.6.89 A16, KSUN + SUSFS + NetHunter + wireless modules)
> gh workflow run "Build OnePlus 13 Kernel" -R Hipuu/OnePlus13-KernelBuilder
>
> # A single variant
> gh workflow run "Build OnePlus 13 Kernel" -R Hipuu/OnePlus13-KernelBuilder \
>   -f kernel_version="6.6.118 A16"
>
> # All seven in parallel
> gh workflow run "Build OnePlus 13 Kernel" -R Hipuu/OnePlus13-KernelBuilder \
>   -f kernel_version=all
> ```
>
> Or use **Actions → Build OnePlus 13 Kernel → Run workflow**.

### Dispatch inputs

| Input | Default | Notes |
|---|---|---|
| `kernel_version` | `6.6.89 A16` | One of the seven variants, or `all`. |
| `ksu_variant` | `KSUN` | `KSUN` or `KSU`. |
| `ksu_branch` | *(empty)* | Empty uses `main` for KSU, `dev` for KSUN, falling back to a pinned compatible commit. |
| `use_susfs` / `susfs_branch` | `true` / *(empty)* | SUSFS feature set; empty branch auto-selects the GKI branch. |
| `nethunter` / `wireless_modules` | `true` | Inline configs / external driver modules. |
| `ddk` / `ddk_injection` | `true` | Bazel-Kleaf build + qcacld injection (6.6.118/6.6.142 A16 only). |
| `optimize_level` / `lto` | `O2` / `thin` | Compiler optimization; `thin` enables the persistent ThinLTO cache. |
| `compiler` | `zycromerz-19` | Or `manifest` for the pinned Clang. |
| `battery_save` / `audit_off` | `false` | Opt-in power knobs. |
| `kernel_uname` | `OP-WILD` | Release-string suffix. |
| `clean_build` / `debug` | `false` | Force a full rebuild / emit debug artifacts. |
| `release_type` | `none` | `none`, `prerelease`, or `release`. |
| `runner` | `github-hosted` | Or `self-hosted`. |

---

## Supported variants

All seven share the SoC (`SM8750`), Android generation (`android15`), and manifest branch (`wild/sm8750`); they differ in kernel version and OxygenOS generation.

| Config | Kernel | OS | Manifest |
|---|---|---|---|
| `configs/OP13-6.6.89.json` | 6.6.89 | A16 | `manifests/a16/oneplus_13_6.6.89_w.xml` |
| `configs/OP13-6.6.118.json` | 6.6.118 | A16 | `manifests/a16/oneplus_13_6.6.118_w.xml` |
| `configs/OP13-6.6.142.json` | 6.6.142 | A16 | `manifests/a16/oneplus_13_6.6.142_w.xml` |
| `configs/OP13-6.6.66.json` | 6.6.66 | A15 | `manifests/a15/oneplus_13_6.6.66_v.xml` |
| `configs/OP13-6.6.30.json` | 6.6.30 | A15 | `manifests/a15/oneplus_13_6.6.30_v.xml` |
| `configs/OP13-CPH-6.6.89.json` | 6.6.89 | A15 global | `manifests/a15/oneplus_13_global_6.6.89_v.xml` |
| `configs/OP13-CPH-6.6.56.json` | 6.6.56 | A15 global | `manifests/a15/oneplus_13_global_6.6.56_v.xml` |

---

## Wireless modules

```bash
# from the extracted module pack, as root
./nethunter-wifi.sh                     # interactive menu
./nethunter-wifi.sh load                # ath9k_htc by default
./nethunter-wifi.sh status
./nethunter-wifi.sh restore             # bring internal Wi-Fi back
./nethunter-wifi.sh conmode monitor 1   # internal Wi-Fi -> monitor mode
./nethunter-wifi.sh conmode sta         # back to normal
```

The loader displaces the platform Wi-Fi stack only when a loaded driver actually needs `mac80211`; a reboot (or `restore`) brings the internal Wi-Fi back.

---

## Battery & power tweaks

An always-on, idempotent, `--forward`-safe patch series (`.github/actions/build-kernel/files/battery/`):

- **Wakelock entropy** — a global 500 ms timeout on new wakelocks, so stray locks cannot pin the CPU awake.
- **s2idle / freeze** — wake once from s2idle; freeze timeout reduced to 1 s.
- **ext4/f2fs** — larger default commit age / `min_fsync_blocks` so writes batch.
- **`alarmtimer` / hrtimer / PCI PME** — minimised wake timeout, no pointless reprogramming, fewer PME wakeups.
- **Log spam** — `devkmsg` and IRQ-affinity spam silenced.
- **Vendor tasktracker** — gate the `oplus_bsp_schedinfo` periodic hrtimer on `tasktrack_enable`.

### Opt-in knobs (both default `false`)

| Knob | What it does | Trade-off |
|---|---|---|
| `battery_save` | `CONFIG_WQ_POWER_EFFICIENT_DEFAULT=y`; appends `rcupdate.rcu_normal_after_boot=1` to `CONFIG_CMDLINE`; blacklists `qcom_cpuss_sleep_stats{,_v4}` | Small latency overhead on workqueue-heavy paths; the sleep-stats drivers cost ~0 battery — blocking them only removes a debugfs node |
| `audit_off` | Appends `audit=0` to `CONFIG_CMDLINE` | On this permissive-SELinux device audit churn is constant; the gain is log hygiene, not battery |

A `service.d` script (`files/battery/99-battery-tweaks.sh`) adds `vm.page-cluster=0` and `oplus_log_level=1` at boot.

---

## Artifacts

| Artifact | Contents |
|---|---|
| `AK3_<MODEL>_<OS>_<KERNEL>_<KSU>_<VER>[_SuSFS_<ver>].zip` | Flashable AnyKernel3 package |
| `Image` | Raw ARM64 kernel image |
| `kernel_modules_<MODEL>_<OS>_<KERNEL>.zip` | External Wi-Fi/CAN modules, flattened `modules.dep`, `nethunter-wifi.sh` |
| `Nethunter-Wireless-Firmware-<VER>.zip` | Re-published unmodified from `nullptr-t-oss/Nethunter-Wireless-Firmware` |
| `qca_cld3_peach_v2.ko` (DDK) | The vendor Wi-Fi module, standalone |
| *(with `debug=true`)* | Build/install logs, `vmlinux`, `Module.symvers`, modules tree |

Modules are built with `CONFIG_MODVERSIONS=y`, so a modules ZIP loads **only** on the exact kernel build it shipped with.

---

## Build caches

Caches live in **GitHub Releases** via the custom `cache/restore` and `cache/save` actions, not `actions/cache`.

| Cache | Bucket | Gating |
|---|---|---|
| ccache | `ccache-cache` | make-based, non-clean builds |
| Bazel disk cache | `bazel-disk-cache` | DDK builds |
| ThinLTO cache | `lto-cache` | make-based + `lto=thin`, non-clean |

Version keys include the KernelSU variant, model, OS version, full kernel version, and Clang fingerprint, so caches never cross-link across toolchains or variants.

---

## Self-hosted runners

Optional. The DDK build completes on GitHub-hosted `ubuntu-latest` (16 GB RAM + the 16 GB swap this action configures). Register a runner with labels `self-hosted`, `linux`, `X64`.

- **RAM** — no ≥32 GB floor. The Bazel JVM heap is clamped to 24 GB (host RAM − 6 GB, floored at 8 GB).
- **Disk** — 150 GiB floor for Bazel DDK variants, 50 GiB otherwise. GitHub-hosted VMs are only warned, since builds have historically fit.
- **Concurrency** — matrix jobs serialise via a top-level `concurrency:` group, since they share the host.

---

## Repository layout

```
.github/
  workflows/build-oneplus13-kernel.yml   # Top-level workflow (workflow_dispatch)
  compatible-commits.json                # Pinned, verified SUSFS/KSUN commits
  actions/
    build-kernel/action.yml              # The actual build (patches, make/Kleaf, packaging)
    build-kernel/files/                  # battery/*.patch, ddk/*, nethunter-wifi.sh
    kernel-source-sync/action.yml        # Downloads pinned sources/toolchains
    cache/restore|save/action.yml        # Release-backed ccache / Bazel / ThinLTO caches
configs/                                 # JSON device configs (one per variant)
manifests/                               # XML repo manifests (one per variant)
```

The published repository is deliberately just the pipeline: `.github/**`, `configs/`, `manifests/`, and `.gitattributes`. Local tooling (`validate_workflow.sh`, `debug_workflow.sh`), `TESTING.md`, and `AGENTS.md` are kept in the working checkout but untracked — see `.gitignore`. (`.gitattributes` stays tracked because it pins `*.sh` to `eol=lf`; the on-device loader does not execute from a CRLF checkout.)

---

## Development

```bash
SKIP_NETWORK=1 bash validate_workflow.sh     # local static validation
./debug_workflow.sh [status|logs|failed|watch|rerun|artifacts|download] [run_id]
```

The validator checks YAML/JSON/XML parseability, DDK **and** battery patch hunk counts, workflow/action structure, config↔manifest alignment, full-SHA pins, absence of boot-image construction, and POSIX/LF conformance of the on-device loader.

To read **compiler** warnings, use the live job log — the post-completion summary omits them, and a real failure can hide there:

```bash
gh api repos/<owner>/<repo>/actions/jobs/<job_id>/logs --allow-escape-sequences
```

> [!NOTE]
> `validate_workflow.sh` and `debug_workflow.sh` are untracked, so a fresh clone has neither the scripts nor any ignore rules. Take care with `git add -A` after a build.

---

> [!IMPORTANT]
> ## Credits
>
> | Project | Author |
> |---|---|
> | **KernelSU** | [tiann](https://github.com/tiann/KernelSU) |
> | **KernelSU-Next** | [rifsxd](https://github.com/KernelSU-Next/KernelSU-Next) |
> | **Magic-KSU** | [5ec1cff](https://github.com/5ec1cff/KernelSU) |
> | **SUSFS** | [simonpunk](https://gitlab.com/simonpunk/susfs4ksu) |
> | **SUSFS Module** | [sidex15](https://github.com/sidex15) |
> | **Baseband Guard** | [vc-teahouse](https://github.com/vc-teahouse/Baseband-guard) |
> | **Droidspaces** | [ravindu644](https://github.com/ravindu644/Droidspaces-OSS) |
> | **Kernel Flasher** | [fatalcoder524](https://github.com/fatalcoder524) |
> | **HMBIRD / Fengchi** | [Numbersf](https://github.com/Numbersf) |
> | **Re-Kernel** | [Sakion-Team](https://github.com/Sakion-Team/Re-Kernel) |
> | **Nethunter Wireless Firmware** | [nullptr-t-oss](https://github.com/nullptr-t-oss/Nethunter-Wireless-Firmware) |
> | **Sultan Kernels** | [kerneltoast](https://github.com/kerneltoast) |
>
> Special thanks to the open-source community for their contributions!

---

> [!WARNING]
> ## Disclaimer
>
> Flashing this kernel will void your warranty, and there is always a risk of bricking your device. Back up your data and understand the risks before proceeding.
>
> **Proceed at your own risk!**
