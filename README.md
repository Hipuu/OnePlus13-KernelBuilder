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
> - 🧠 **BORE scheduler** — opt-in burst-time scoring on top of EEVDF, so interactive work wins under load. A scheduler change, so it stays off by default. See [BORE scheduler](#-bore-scheduler).
> - 🎲 **LRNG v60** — opt-in replacement for the kernel RNG: independent IRQ/CPU/Jitter entropy sources feeding a ChaCha20 DRBG instead of one BLAKE2s pool. An entropy-subsystem change, so it stays off by default. See [LRNG v60](#-lrng-v60).
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
| `optimize_level` / `lto` | `O2` / `thin` | Compiler optimization. `thin` enables the persistent ThinLTO cache; `none` maps to `CONFIG_LTO_NONE` and is now asserted in the resolved `.config` (see note below). |
| `compiler` | `zycromerz-19` | Or `manifest` for the pinned Clang. |
| `battery_save` / `audit_off` | `false` | Opt-in power knobs. |
| `bore` | `false` | Opt-in BORE scheduler patch — **6.6.118/6.6.142 only** (not "A16": 6.6.89 A16 also fails), see [BORE scheduler](#-bore-scheduler). |
| `lrng` | `false` | Opt-in LRNG v60 RNG replacement — all seven variants, see [LRNG v60](#-lrng-v60). |
| `kernel_uname` | `OP-WILD` | Release-string suffix. |
| `clean_build` / `debug` | `false` | Force a full rebuild / emit debug artifacts. |
| `release_type` | `none` | `none`, `prerelease`, or `release`. |
| `runner` | `github-hosted` | Or `self-hosted`. |

> **On `lto`:** the three Kconfig choice members are `LTO_NONE`,
> `LTO_CLANG_THIN` and `LTO_CLANG_FULL` — there is no `LTO_CLANG_NONE`. The
> action previously wrote `-e LTO_CLANG_NONE`, which `scripts/config` appends
> verbatim and `olddefconfig` then discards as unknown, so the request was
> carried only by the choice *default* rather than by intent. `none` now sets
> `LTO_NONE` explicitly, and the resolved `.config` is asserted for every mode
> ([palazik `b4777f8`](https://github.com/palazik/actions_oplus_sm8750/commit/b4777f8f022741b810e9ea073292e8eca71a0ac3)).

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
> [!IMPORTANT]
> ## BORE scheduler
>
> **BORE** (Burst-Oriented Response Enhancer, v7.0.0, by Masahito Suzuki) scores each task by its *burst time* — the runtime since it last slept or yielded — and boosts the shorter, burstier ones. That is the profile of a compositor, a launcher, a game loop or a scroll gesture, so interactive work keeps its latency while a background compile or download runs. It costs some scheduling fairness, which is why it is a knob and not a default.
>
> Enable it with `bore=true`. The step applies `.github/actions/build-kernel/files/bore/bore-7.0.0.patch` with `--fuzz=0` and appends `CONFIG_SCHED_BORE=y` to the common `gki_defconfig`. It runs before every other patch that touches `kernel/sched/`, so its hunks always see the freshly synced tree.
>
> **Provenance.** A backport of firelzrd's canonical `patches/stable/0001-linux6.12.37-bore-7.0.0.patch` to this tree's 6.6 EEVDF. firelzrd ships 7.0.0 only for 6.12/6.18/7.1/7.2/7.3; 34 of its hunks reject against 6.6 because they depend on the 6.12 EEVDF (`do_preempt_short`, `do_preempt_weight`, `protect_slice`, `cancel_protect_slice`, `requeue_delayed_entity`, `rel_deadline`, `custom_slice`, `sched_delayed`). Each rejected site was re-derived here against the 6.6 forms — `protect_slice()` is inlined as `vlag == deadline`, and `place_entity()`'s deferred `vslice` replaces the `rel_deadline` dance — and the `ctl_table` handler signatures were changed from `const struct ctl_table *` (6.12) to `struct ctl_table *` (6.6). Everything else is firelzrd's text.
>
> **Why 7.0.0 and not the older 5.9.7.** The previously vendored 5.9.7 patch (palazik/brokestar233) **bootlooped on device**. The embedded-config delta between a booting kernel and that one was exactly `CONFIG_SCHED_BORE=y` plus `CONFIG_MIN_BASE_SLICE_NS=2000000` — nothing else — so the fault was in 5.9.7 itself against this tree's newer `fair.c`. 7.0.0 is the maintained line and restructures the burst accounting onto static keys and a per-task `bore_ctx`.
>
> **The one deliberate deviation from firelzrd's text: ABI-neutral storage.** Upstream 7.0.0 embeds `struct bore_ctx bore;` *inline* in `task_struct`, which grows the struct. genksyms CRCs propagate transitively through the type graph, so that shifts the CRC of every exported symbol whose signature reaches `task_struct` (e.g. `hci_dev → sk_buff → sock → mem_cgroup → task_struct`), and ~430 symbols disagree between the GKI and vendor `Module.symvers`. The vendor tree has its **own** `include/linux/sched.h` and is not patched, so with `ddk=true` modpost then dies with `<sym> exported twice. Previous export was in <same .ko>` in `//vendor/qcom/opensource/wlan/platform` (proved 2026-10-13: `bore=true ddk=true` fails, `bore=false ddk=true` passes, all 36 `OP_*` variables otherwise identical). So the ctx is a **pointer** in `sched_entity`'s free KABI reserve slot 1: `android_kabi.h` expands `_ANDROID_KABI_REPLACE` to bare `_orig` under `__GENKSYMS__`, so the slot stays an 8-byte `u64` for CRC purposes and the layout never moves — confirmed by running the real `scripts/genksyms/genksyms`, which yields an identical CRC for a `task_struct`-taking symbol (and for a `sched_entity`-taking one) with BORE on and off. This is how palazik's 5.9.7 did it, and why 5.9.7 never hit the failure. The ctx is `kzalloc`'d in `dup_task_struct` and freed in `free_task`; the step asserts the slot is claimed and that no inline field reappeared, because losing it silently reintroduces the ABI break.
>
> **Why `sched_entity` and not a `task_struct` slot.** Both are ABI-neutral, but `task_struct`'s free KABI slots are all inside the context of the Droidspaces SYSVIPC patch, which the pipeline applies later and which first probes with `patch -R --dry-run` to see whether it is already applied. Claiming a `task_struct` slot makes that probe fail, so the step falls through to a forward apply that hard-fails under `set -e` (this is what killed run 38541385542, ~3.5 minutes in — long before any compile). Around 30 patches in `WildKernels/kernel_patches` touch `task_struct`'s KABI region; **none** touches `sched_entity`'s, so slot 1 is collision-free. `sched_entity` precedes `struct bore_ctx` in the header, so the patch adds a `struct bore_ctx;` forward declaration above it.
>
> **Scope.** **6.6.118 and 6.6.142 only — not "A16".** The label is misleading: 6.6.89 is also an A16 variant, but its `fair.c` still carries the older EEVDF shape (`sysctl_sched_base_slice = 750000ULL`, `0.75 msec`), which BORE's rewrite hunks replace, and no fuzz factor bridges that gap. So both the five A15 trees *and* 6.6.89 A16 fail the strict dry-run, and asking for `bore=true` there **fails the build** with the offending hunks printed, rather than half-patching the tree. Note that 6.6.89 A16 is the default dispatch variant, so this is the first thing a `bore=true` user hits if they do not name a version.
>
> **At runtime** the tunables are sysctls under `/proc/sys/kernel/` (`sched_bore`, `sched_burst_inherit_type`, `sched_burst_protect_slice_lv`, `sched_credit_cap_us` and the other `sched_burst_*` knobs, mode `0644`). `echo 0 > /proc/sys/kernel/sched_bore` puts the fair class back to plain EEVDF without reflashing.
>
> **Status.** Verified to apply with `--fuzz=0` and to compile against both 6.6.118 and 6.6.142 (locally, `kernel/sched/*.o` plus `fork.o`/`exit.o`/`futex/waitwake.o`, with BORE both on and off), and `validate_workflow.sh` re-checks its hunk counts.
>
> **Re-verified by compilation** (clang 22 / `LLVM=1`, aarch64). On **both** supported trees the patch applied at `--fuzz=0` and these objects all built:
>
> | tree | bore.o | core.o | fair.o | fork.o | exit.o |
> |---|---|---|---|---|---|
> | 6.6.118 | 179 KB | 939 KB | 497 KB | 452 KB | 312 KB |
> | 6.6.142 | 179 KB | 930 KB | 497 KB | 463 KB | 313 KB |
>
> The step's post-apply assertions were checked against both patched trees and all hold — `config SCHED_BORE` in `init/Kconfig`, `sched_init_bore` in `core.c`, the `bore.h` include, `sched_update_min_base_slice` in `sched.h`, the `ANDROID_KABI_USE(1, struct bore_ctx *bore)` slot claimed, the forward declaration present, and **no inline `bore_ctx`** (the ABI guarantee). `CONFIG_SCHED_BORE=y` survived `olddefconfig` on both.
>
> On `6.6.89` the patch correctly **rejects** at `--fuzz=0` (2 of 19 hunks), which is the documented fail-fast scope.
>
> It has **not been booted** — a green compile proves it links, not that the scheduler is sound at runtime.

---

> [!IMPORTANT]
> ## LRNG v60
>
> **LRNG** (Linux Random Number Generator, v60, by Stephan Mueller) replaces `drivers/char/random.c` with a different architecture: independent entropy sources — interrupt timing, per-CPU timing, and optionally Jitter and the scheduler — each credited separately, feeding a ChaCha20 DRBG. The stock kernel funnels everything into a single BLAKE2s pool. The practical difference is faster, better-credited boot entropy: the LRNG reports a fully seeded DRNG early instead of waiting for the pool to reach its threshold, which matters on a phone that has to have a usable `/dev/random` before user space is far along.
>
> Enable it with `lrng=true`. The step applies `.github/actions/build-kernel/files/lrng/lrng_v60_android15-6.6.patch` with `--fuzz=0` and appends `# CONFIG_RANDOM_DEFAULT_IMPL is not set` plus `CONFIG_LRNG=y` to the common `gki_defconfig`.
>
> **Provenance.** Ported from [`palazik/kernel_patches`](https://github.com/palazik/kernel_patches) blob `7567dc0c`, which consolidates upstream [`smuellerDD/lrng`](https://github.com/smuellerDD/lrng) `kernel_patches/v6.18` with `backports/v60-6.6.119` for the android15-6.6 GKI. The vendored copy is sha256 `17a872f6f160004c26ab025409656f8b6df394f4e1dedbda3766b182f8c47c3a`. It modifies five files (`crypto/drbg.c`, `drivers/char/Kconfig`, `drivers/char/Makefile`, `include/crypto/drbg.h`, `kernel/sched/core.c`) and creates 56 under `drivers/char/lrng/`, 71 hunks in all.
>
> **Variants.** All seven — A15 and A16. Unlike BORE, the LRNG patch does not rewrite version-specific scheduler code: its only `kernel/sched/core.c` hunks are an include line and a two-line call in `ttwu_stat`, both of which sit on anchors every pinned tree shares. Applicability is checked by strict dry-run against all seven trees, by fetching the five modified files at each pin.
>
> **Config shape.** The patch gates `random.o` behind a new `CONFIG_RANDOM_DEFAULT_IMPL` (default `y`) and adds `CONFIG_LRNG` (default `n`). Turning the former off makes `LRNG_RANDOM_IF` — which defaults on exactly when it is off — provide `/dev/random`, `/dev/urandom` and `getrandom(2)` from the LRNG, and pulls in the ChaCha20 DRBG and the sysctl interface. If the Kconfig source line or the Makefile gate were ever lost, `olddefconfig` would drop both symbols as unknown and the tree would build with **no RNG at all** (the link dies on `get_random_bytes`), so the step greps the plumbing that proves the patch landed where it had to, and both build paths assert `CONFIG_LRNG=y` in the resolved `.config`.
>
> **What stays off.** `LRNG_SCHED` (the scheduler entropy source) is left at its `n` default: it adds a call on every context switch, and the IRQ and CPU sources are on by default and sufficient. The optional `/dev/lrng` device and the kernel-crypto-API/hwrng registrations are likewise off.
>
> **At runtime** `/proc/sys/kernel/random/` is the LRNG's own table (`entropy_avail`, `poolsize`, `write_wakeup_threshold`, `boot_id`, `uuid`, `urandom_min_reseed_secs`), and `/proc/lrng_type` prints the DRNG name, security strength and per-source state. `dmesg | grep -i lrng` shows `LRNG minimally seeded with N bits of entropy` and `LRNG fully seeded with N bits of entropy`.
>
> **Status.** Applies cleanly with `--fuzz=0` on all seven pinned trees, composes with BORE in either order, and `validate_workflow.sh` re-checks its hunk counts.
>
> **Compiled** (clang 22 / `LLVM=1`, aarch64, 6.6.89). The patch applied at `--fuzz=0`, all six of the step's plumbing assertions held, and `make rc=0` produced the whole RNG: 15 objects under `drivers/char/lrng/` plus `crypto/drbg.o`, `kernel/sched/core.o` and `init/main.o`. The resolved config matches the intent above — `CONFIG_LRNG=y`, `# CONFIG_RANDOM_DEFAULT_IMPL is not set`, `LRNG_DRNG_CHACHA20=y`, `LRNG_IRQ=y`, `LRNG_CPU=y`, and **`LRNG_SCHED` off**.
>
> The failure mode that actually matters was checked directly rather than assumed: `random.o` and `lrng_interface_random_kernel.o` both define `get_random_bytes`, so a build linking both would die on duplicate symbols. Inspecting `drivers/char/built-in.a` shows **`random.o` is absent** — the `obj-$(CONFIG_RANDOM_DEFAULT_IMPL)` gate keeps it out, leaving the LRNG interface as the sole provider of `get_random_bytes`, `add_device_randomness` and `add_input_randomness`. That is exactly what the "would build with no RNG" warning below is about, now verified end-to-end instead of by grep.
>
> It has **not been booted** — a compile proves the RNG links and the config resolves, not that entropy is credited correctly at runtime or that `/dev/random` behaves. `dmesg | grep -i lrng` on a real boot is still the check that matters.

---

## SUSFS / KernelSU version pairing

SUSFS has a kernel half *and* a KernelSU half, and the two must agree on the
`selinux_hide` dispatch shape. The pins live in
`.github/compatible-commits.json`, keyed per KSU variant.

| | Pinned | Notes |
|---|---|---|
| SUSFS (`android15-6.6`, both variants) | `f98f0822` — **v2.4.0** (2026-11-15) | `SUSFS_VERSION` read from `kernel_patches/include/linux/susfs.h` at that ref |
| KernelSU (`main`) | `64833c00` | "feat(kernel): Add LSM hook for inode_setxattr (#3996)" |
| KernelSU-Next (`dev`) | `7ccf6147` | same LSM hook, carried into KSUN |

v2.4.0's commit message names `64833c00` as its KernelSU sync point, so the
SUSFS and KernelSU pins are matched by construction rather than by guesswork.
Pairing v2.4.0 with an older KernelSU still applies the kernel-side half, but
the `susfs_selinux_inode_setxattr` / `_post_setxattr` handlers it registers
have nothing to dispatch into.

**v2.3.0/v2.4.0 are KSUN-capable.** An earlier revision of the action
hard-failed `v2.3.0` + KSUN on the belief that WildKernels' fix patch sets
stopped at v2.2.0. They do not: `next/susfs_fix_patches/v2.3.0/` and `v2.4.0/`
both exist upstream and both ship the same `fix_<file>.patch` set plus
`overwrite_hook_mode.patch` and `ksu_toolkit.patch`. The fix patches are
resolved from the detected `$susfs_version`, so a v2.4.0 pin automatically
pulls the v2.4.0 fix set — there is no cross-version mix.

### Does the pin fit the trees?

Checked by fetching each of the 27 files the v2.4.0 patch touches from the tree
the manifest actually names — including the `hipuu` remote for 6.6.142, which is
the only variant whose common kernel is not from OnePlusOSS — applying the
prepatch this repo already performs, then dry-running the whole patch:

| Variant | v2.4.0 patch |
|---|---|
| `6.6.30_v` | CLEAN |
| `6.6.66_v` | CLEAN |
| `global_6.6.56_v` | CLEAN |
| `global_6.6.89_v` | CLEAN |
| `6.6.118_w` | CLEAN |
| `6.6.142_w` | CLEAN |
| `6.6.89_w` | CLEAN |

Two hunks need the prepatch, and both mutations were already there:
`fs/proc/task_mmu.c` (#6) is cured by the `nr_subpages`/`res` declarations and
the `VMA_PAD_START` fallback, and `fs/namespace.c` (#1) by the
`#include <trace/hooks/fs.h>` insertion the action performs for a15-6.6.

> 6.6.30 fails two hunks if the prepatch is skipped. That is **pre-existing** —
> the previous v2.2.0 pin fails the same two hunks on the same tree before
> prepatch — so it is not a consequence of this pin bump.

This is source-level validation only. Nothing here was compiled.

To move the pins, change all four values together and re-run a build; the
`Resolve SUSFS ref` / `Resolve KernelSU ref` steps turn any unresolvable ref
into a hard failure before compilation starts.

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

Modules are built with `CONFIG_MODVERSIONS=y`, so a modules ZIP loads **only**
on the exact kernel build it shipped with.

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

## Known issues

### SUSFS v2.3.0/v2.4.0 are KSU-only — KSUN stays on v2.2.0

SUSFS v2.4.0's KernelSU half does **not** fit KernelSU-Next, despite
`next/susfs_fix_patches/v2.4.0/` shipping a `fix_<file>.patch` for every file
the base patch touches. Those fix patches are authored against
**tiann/KernelSU's** file layout, not KernelSU-Next's.

Measured, not assumed — CI run `44007359941` pinned v2.4.0 with KSUN and failed
all seven variants at the patch stage:

```
kernel/Kbuild.rej
kernel/core/init.c.rej             6 of 8 hunks
kernel/feature/kernel_umount.c.rej
kernel/feature/sucompat.c.rej      2 of 4 hunks
kernel/hook/setuid_hook.c.rej
kernel/selinux/rules.c.rej
kernel/supercall/dispatch.c.rej    2 of 9 hunks
```

So the pins are split by variant, and **both are CI-verified**:

| Variant | SUSFS | KernelSU | Verified by |
|---|---|---|---|
| KSU | `f98f0822` (v2.4.0) | `f49be695` (tiann/KernelSU main) | run `44012089375`, all 7 variants → `..._KSU_32731_SuSFS_v2.4.0.zip` |
| KSUN | `be7b7ef4` (v2.2.0) | `234f6e04` (KernelSU-Next dev) | run `44020861237` → `..._KSUN_33239_SuSFS_v2.2.0.zip` |

The two paths are structurally different, which is why they need different
revisions:

- **KSU** applies the enable patch cleanly on its own — no fix set involved at
  v2.4.0. Found by applying the 106-hunk patch against 60 commits of
  `tiann/KernelSU` with a hard-reset worktree each time; only `f49be695` (newest)
  and `64833c00` are clean.
- **KSUN** never applies cleanly on its own. It is designed to apply the patch
  best-effort (6 rejects at v2.2.0), then repair each rejected file with
  `fix_<file>.patch`, then `overwrite_hook_mode` + `ksu_toolkit`. Bumping it to
  `7ccf6147` was tried and broke it (53 failed hunks, no fix set covers them);
  reverted to `234f6e04`.

> **Bug found by CI, not by review.** The KSUN repair loop had always worked, but
> it left each `.rej` file in place. The stricter reject scan added earlier in
> this work then failed KSUN builds for the very files the loop had just
> repaired — it had no way to tell a repaired file from an unrepaired one. The
> loop now deletes each `.rej` only when its fix patch succeeds, so a genuinely
> unrepairable file still fails loudly. Without running the build, this would
> have shipped as "KSUN is broken" with the wrong cause.

A previous revision of this action removed the KSU-only guard for v2.3.0+ on the
reasoning that the fix sets made those versions KSUN-capable. That was wrong
(run `44007359941` left rejects in `kernel/core/init.c` 6/8, `sucompat.c` 2/4,
`supercall/dispatch.c` 2/9 and four more). The guard is back, and the reject scan
now also covers the KSUN tree.

### `CONFIG_KSU_SUSFS_ENABLE_LOG` is load-bearing

SUSFS v2.4.0's `fs/susfs.c` defines `SUSFS_LOGI`/`SUSFS_LOGE` fallbacks in the
`#else` branch of `#ifdef CONFIG_KSU_SUSFS_ENABLE_LOG`, but **not**
`SUSFS_LOGI_RL`/`SUSFS_LOGE_RL`, which are called unconditionally at lines 78
and 155. Turning `CONFIG_KSU_SUSFS_ENABLE_LOG` off therefore fails to compile
with `call to undeclared function 'SUSFS_LOGI_RL'`.

This pipeline always sets it to `y`, so builds are unaffected. The reason it is
recorded here is that the line looks like a tunable log switch, and it is not —
removing it breaks the build. Upstream bug in `simonpunk/susfs4ksu` at
`f98f0822`; not worked around locally.

### `CONFIG_UKSM_CPU_GOVERNOR` / `_SLEEP_MS` must stay pinned

The 6.6.142 Hipuu perf-stack fork declares the UKSM choice and integer symbols
with **no defaults**. Left unpinned they are `(NEW)`, and any `syncconfig` that
sees them prompts interactively — under CI (stdin at EOF) that becomes an
error-retry loop that looks exactly like a hung "Creating kernel config" step.

Measured on the real 6.6.142 tree, stdin at EOF:

| | `UKSM_CPU_GOVERNOR` | `UKSM_SLEEP_MS` |
|---|---|---|
| without the pins | *(empty)* — invalid value | 8 |
| with the action's pins | `4` | `100` |

So the pins are not cosmetic tidy-up; without them the resolved config contains
an invalid empty governor. The action's `CONFIG_UKSM_CPU_GOVERNOR=4` /
`CONFIG_UKSM_SLEEP_MS=100` lines, and the comment above them, are load-bearing —
do not remove them.

---

## Development

```bash
python3 .validate_ci.py                     # static validation (needs pyyaml)
python3 .validate_ci.py --tree ~/kernel     # + check every CONFIG_* against Kconfig
python3 .check_config_e2e.py ~/kernel gki_defconfig \
        .github/actions/build-kernel/action.yml    # + real olddefconfig round-trip
python3 .negative_test_ci.py                # proves the validator fails on regressions
KERNEL_TREE=~/kernel python3 .negative_test_ci.py   # + the 4 config-symbol cases
./debug_workflow.sh [status|logs|failed|watch|rerun|artifacts|download] [run_id]
```

`.validate_ci.py` checks YAML/JSON/XML parseability, `bash -n` on every `run:`
block in the workflow and the composite actions, config↔manifest alignment,
full-40-char-SHA pins (and that `android15-6.6` is pinned for both KSU
variants), POSIX `sh -n` + LF + no-`awk` conformance of the on-device loader,
absence of boot-image construction, and that neither the retired
`LTO_CLANG_NONE` symbol nor the retired SUSFS-v2.3.0-KSUN gate has been
reintroduced.

With `--tree`, it additionally resolves every `CONFIG_*` the action appends
against the kernel's real Kconfig files. This catches the class of bug where a
mistyped or non-existent symbol is discarded by `olddefconfig` without a word,
so the build is green but the feature is missing. Six such lines were found and
fixed this way (§ *Dead config symbols* below).

`.check_config_e2e.py` goes one step further and is the strongest config check
available without compiling: it resolves the stock `gki_defconfig` with the
kernel's **own** `scripts/kconfig`, appends the action's lines, runs
`olddefconfig`, and diffs. That catches symbols which are defined and whose
request is still not honoured — e.g. a `=y` request on a child of a modular
parent, which Kconfig silently demotes to `=m`, or a symbol gated behind an
`if` whose controlling option is off (§ *Symbols that existed but never took*).

Both need a kernel checkout and, for `kconfig`, a cross-compiler shim because
Kconfig probes `$(CC)` even when it is only building its own config tool.

`.negative_test_ci.py` injects each regression the validator claims to catch
into a throwaway copy of the tree and asserts the validator fails with the
expected message, plus positive controls that the unmodified tree passes. Nine
cases run without a kernel tree; four more run when `KERNEL_TREE` is set. A
check that cannot fail is worse than no check, so this is what keeps the
validator honest.

It does **not** apply kernel patches or compile anything — it is a static gate,
not a build.

### What has actually been compiled

**Full CI builds.** The authoritative evidence is no longer local: workflow run
[`44012089375`](https://github.com/Hipuu/OnePlus13-KernelBuilder/actions/runs/44012089375)
built **all seven variants** green on GitHub-hosted runners, KSU variant, SUSFS on:

| Variant | Time | Artifacts |
|---|---|---|
| `OP13-6.6.89` (A16) | 30m23s | `AK3_OP13_A16_android15-6.6.89_KSU_32731_SuSFS_v2.4.0.zip` + modules ZIP |
| `OP13-6.6.66` (A15) | 34m21s | `AK3_OP13_A15_android15-6.6.66_KSU_32731_SuSFS_v2.4.0.zip` + modules ZIP |
| `OP13-6.6.142` (A16) | 43m03s | `AK3_OP13_A16_android15-6.6.142_KSU_32731_SuSFS_v2.4.0.zip` + modules ZIP |
| `OP13-6.6.30` (A15) | 43m40s | `AK3_OP13_A15_android15-6.6.30_KSU_32731_SuSFS_v2.4.0.zip` + modules ZIP |
| `OP13-6.6.118` (A16) | 35m33s | `AK3_OP13_A16_android15-6.6.118_KSU_32731_SuSFS_v2.4.0.zip` + modules ZIP |
| `OP13-CPH-6.6.89` (A15) | 28m21s | `AK3_OP13-CPH_A15_android15-6.6.89_KSU_32731_SuSFS_v2.4.0.zip` + modules ZIP |
| `OP13-CPH-6.6.56` (A15) | 29m46s | `AK3_OP13-CPH_A15_android15-6.6.56_KSU_32731_SuSFS_v2.4.0.zip` + modules ZIP |

A separate KSUN run
([`44020861237`](https://github.com/Hipuu/OnePlus13-KernelBuilder/actions/runs/44020861237))
built `OP13-6.6.89` in 30m30s, producing
`AK3_OP13_A16_android15-6.6.89_KSUN_33239_SuSFS_v2.2.0.zip`. Both flavours are
therefore confirmed end to end, not just the default one.

The 6.6.89 job log confirms the versions that actually shipped:
`SusFS Version: v2.4.0`, `KernelSU Version: 32731`, `✅ SUSFS patches applied`,
and a modules ZIP whose dependency closure completed.

That run also **found a real bug**, which is the point of running it: the first
attempt (run `44007359941`, KSUN) failed all seven variants at the SUSFS patch
stage. See § *Known issues* — the KSUN pairing is reverted.

Static checks are cheap; compilation is not. What was also built locally (clang
22, `LLVM=1`, `ARCH=arm64`, on the real OnePlusOSS 6.6.89 and 6.6.118 trees):

| What | Result |
|---|---|
| `gki_defconfig` + the action's 181 config appends, through the kernel's own `olddefconfig` | 7,993-line config, **0 mismatches** (155 took as requested, 26 patch-added) |
| ~1,168 objects across `kernel/sched`, `mm`, `fs`, `net`, `net/mac80211`, `net/wireless` | clean |
| The individual files the pipeline patches — `mm/vmscan.o`, `mm/memory.o`, `mm/zsmalloc.o`, `fs/namespace.o`, `fs/exec.o`, `fs/proc/base.o`, `fs/proc/task_mmu.o`, `fs/readdir.o`, `kernel/sched/core.o`, `kernel/sched/fair.o`, `net/unix/af_unix.o` | all built |
| BORE 7.0.0 on 6.6.118 — `bore.o`, `core.o`, `fair.o`, `fork.o`, `exit.o` + all six patch-landing assertions | **compiles**, ABI-neutral |
| BORE 7.0.0 on **6.6.142** (Hipuu perf-stack) — same five objects, same assertions | **compiles**, ABI-neutral |
| BORE 7.0.0 on 6.6.89 | correctly rejects (2/19 hunks) — documented scope |
| LRNG v60 on 6.6.89 — 15 `lrng/*.o` + `crypto/drbg.o` + `kernel/sched/core.o` + `init/main.o` | **compiles**, `random.o` correctly excluded so the RNG interface has one provider |
| **KernelSU + SUSFS on 6.6.89** — 34 `drivers/kernelsu/**/*.o` + `fs/susfs.o` (405 KB) | **compiles** — the core combination, including every file the SUSFS→KernelSU patch touches |
| **Wireless module set on 6.6.89** — 48 module objects, modpost-resolved | **every advertised driver built**: ath9k_htc, ath10k_usb, carl9170, rtl8187, rtl8xxxu, rtw88, rt2800usb (rt2x00), zd1211rw, p54usb, mt76 + `cfg80211`/`mac80211` |
| **Module-pack dependency closure** — 6 offline cases | **algorithm verified**: resolves 13 modules incl. out-of-seed `eeprom_93cx6`/`libarc4`/`rfkill`, terminates on cycles, keeps deps with no map entry, and confirms the `modules.dep` line-count gate is unreachable by construction |

The KernelSU/SUSFS build is worth a note because it exposed a real ordering
constraint and one upstream bug:

- `drivers/kernelsu` will not compile until `security/selinux/` has run, because
  `objsec.h` includes `flask.h`/`av_permissions.h`, which `genheaders` *generates*.
  A partial `make drivers/kernelsu/` on a clean tree therefore fails with
  `'flask.h' file not found` — a build-ordering artifact, not a source defect.
- SUSFS v2.4.0's `fs/susfs.c` calls `SUSFS_LOGI_RL` unconditionally but defines
  it only under `#ifdef CONFIG_KSU_SUSFS_ENABLE_LOG`; the `#else` branch supplies
  fallbacks for `SUSFS_LOGI`/`SUSFS_LOGE` only. Building with logging **off**
  therefore fails with `call to undeclared function 'SUSFS_LOGI_RL'`. This
  pipeline always sets `CONFIG_KSU_SUSFS_ENABLE_LOG=y`, so it takes the safe
  branch and is unaffected — but it is an upstream hazard worth knowing if that
  config line is ever turned off.

**Not built:** a full `vmlinux`/`Image`, the linked module set (`.ko`), and the
DDK/Kleaf path (needs Bazel). This box has 4 GB RAM; a full build wants ~16 GB,
and the slice above was already stopped at a 50-minute timeout while still
compiling happily. All three kernel trees have now been exercised: 6.6.89
(base + LRNG + KernelSU/SUSFS + modules), 6.6.118 and 6.6.142 (BORE).

On the modules: every advertised driver **compiled and passed modpost**, which is
where a missing dependency or an unresolved symbol would surface. The final `.ko`
link only runs under the whole-tree `modules` target, which needs the full build,
so what is proven is "compiles and resolves", not "the `.ko` exists".

The packager's **closure algorithm** is separately tested offline
(`.test_module_closure.py`, 6 cases), including the regression that motivated
using `modinfo` instead of `modules.dep`: a truncated `modules.dep` loses
`rtl8187 → eeprom_93cx6` and ships a module that cannot `insmod`, while the
modinfo-derived closure keeps it. What is *not* tested is the packager
end-to-end, since that needs real `.ko` files.

One defensive check was audited and found **unreachable**, which is the outcome
you want: the step fails the build when `wc -l modules.dep` differs from the
staged module count. That can only happen if a staged module has no dependency
map entry, but `STAGE` is populated solely from `WANTED`, `WANTED` derives from
the map keys plus the seeds, and the map is built by scanning the same
`UNAME_DIR` the seeds come from — so every seed necessarily has an entry. The
gate is a sound backstop, not a lurking failure.

**Not tested at runtime.** Nothing has been booted on a device. A clean compile
proves the code is syntactically valid and links against the tree's own headers;
it says nothing about whether a scheduler, a root implementation or a hiding
feature behaves correctly on hardware.

### A caveat about the audit tooling itself

Three rounds of "every appended symbol resolves" were run with a regex of
`[A-Z0-9_]` for Kconfig symbol names. Kconfig names are `[A-Za-z0-9_]*`, so any
symbol containing a lowercase letter — `MT76x0U`, `MT76x2U` — was silently
**skipped** rather than checked. The conclusion happened to still hold once fixed
(187 symbols audited instead of 181, still 0 mismatches), but the earlier
coverage was incomplete. `.test_symbol_regex.py` now pins the pattern so the blind
spot cannot return.

For compiler warnings, use the live job log — the post-completion summary
omits them, and a real failure can hide there:

```bash
gh api repos/<owner>/<repo>/actions/jobs/<job_id>/logs --allow-escape-sequences
```

> [!NOTE]
> The local kernel-tree tooling (`validate_workflow.sh`, `debug_workflow.sh`),
> `TESTING.md`, and `AGENTS.md` are kept in the working checkout but untracked
> — see `.gitignore`. `.validate_ci.py`, `.negative_test_ci.py`, and
> `.audit_config_keys.py` are likewise untracked. Take care with `git add -A`
> after a build.

### Dead config symbols

Verified against the real OnePlusOSS 6.6.89 common tree
(`grep -rE '^config <SYM>$' --include='Kconfig*'`), six lines this action
appended had no Kconfig definition and were therefore silently dropped:

| Line | Why it never worked | Resolution |
|---|---|---|
| `CONFIG_CAN_ESD_USB2=m` | typo — 6.6 defines `CAN_ESD_USB` | renamed, so the ESD CAN adapter is now actually built |
| `CONFIG_NETFILTER_XT_TARGET_REJECT=y` | no such symbol; `net/netfilter/xt_REJECT.c` does not exist in this tree | replaced with the real per-family `IP_NF_TARGET_REJECT` / `IP6_NF_TARGET_REJECT` |
| `CONFIG_CC_OPTIMIZE_FOR_PERFORMANCE_O3` | the -O3 choice member was removed upstream in 5.18 | removed; `-O3` is passed via `KCFLAGS_EXTRA`, which is what reaches the compiler |
| `CONFIG_OPTIMIZE_INLINING=y` | removed in 6.x (forced on since 5.15) | removed |
| `CONFIG_KSM_LEGACY=n` | never existed in this tree | removed |
| `CONFIG_ANDROID_BINDER_IPC_RUST=m` | tree defines `ANDROID_BINDER_IPC*` but no `_RUST` variant | removed (dormant — `rust_build` is false everywhere) |

`CAN_ESD_USB2` is the one with user-visible impact: the ESD CAN/USB adapter was
listed as supported but had never been compiled.

### Symbols that existed but never took

Running the kernel's own `olddefconfig` over the real defconfig (not just name
matching) found two more, where the symbol *is* defined and the request was
still silently not honoured:

| Symbol | What actually happened | Resolution |
|---|---|---|
| `CONFIG_BT_HCIBTUSB` and the other `BT_HCI*` | requested `=y`, but they are `tristate ... depends on USB`, and `CONFIG_BT` is `=m` in this tree — a `=y` child of a modular parent is unsatisfiable, so Kconfig demoted each to `=m` | request `=m` explicitly; the modules still ship in the modules ZIP |
| `CONFIG_USB_AIRSPY`, `CONFIG_USB_HACKRF` | never built. Both live under `if MEDIA_SDR_SUPPORT`, which is `default y if !MEDIA_SUPPORT_FILTER` — off here, so the entries were never even offered to Kconfig | enable `CONFIG_MEDIA_SDR_SUPPORT=y`; the SDR support the feature list advertises now exists |

`CONFIG_USB_AIRSPY`/`USB_HACKRF` is the significant one: NetHunter SDR was
documented but absent from every build.

Verified end-to-end with `.check_config_e2e.py` — stock `gki_defconfig` →
append the action's 181 lines → `olddefconfig` → compare: **155 took as
requested, 26 patch-added (exempt), 0 mismatches**.

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
