# OnePlus 13 battery-optimization patch research

Research date: 2026-08-15

Repository baseline: `c36e4ae0b917a40db86b9395d67f30d727bf9114`

Primary target: OnePlus 13 (`sun`, SM8750), Android 16, Linux 6.6.118

Status: research handoff only; no kernel, workflow, manifest, or configuration change was made

## Executive conclusion

There is no honest way to promise that a kernel patch both saves a measurable amount of battery and has literally zero performance effect before testing it on the phone. For this project, "does not affect performance" should mean that an A/B test shows no statistically meaningful regression in throughput, responsiveness, frame pacing, storage, radio behavior, or sustained thermals.

The source audit found only two candidates that are narrow enough to approach that standard:

1. **Do not start OnePlus's task-tracking hrtimer while task tracking is disabled.** This is the best device-specific candidate. The shipped `oplus_bsp_schedinfo.ko` initializes an exact periodic hrtimer even though `tasktrack_enable` defaults to zero. Its callback keeps rearming while the actual update routine immediately exits. The existing proc enable path already starts and stops the timer, so gating the initial start preserves enabled behavior.
2. **Backport upstream stable commit `ece8be21d8c93`**, `hrtimer: Avoid pointless reprogramming in __hrtimer_start_range_ns()`. It skips a redundant clock-event reprogram while the hrtimer interrupt is already running. It applies cleanly to the exact pinned OnePlus common source and does not change timer expiry semantics. Expected battery impact is small.

A third idea, **RCU lazy callback batching**, has a stronger upstream power rationale but is not a no-risk patch. Support is already compiled in and disabled by default. The pinned source also lacks a known hurry-callback fix that can otherwise delay Android Update Engine work for seconds. Treat RCU lazy as a separate, higher-risk experiment only after applying and validating that fix.

Do **not** change CPU/GPU operating points, scheduler boosts, HMBIRD policy, thermal limits, cpuidle state parameters, display refresh behavior, Wi-Fi power-save timings, modem policy, or charger logic for this goal. Those changes can trade performance, latency, reliability, radio behavior, battery longevity, or safety for energy.

The current broad `opt` patch bundle also contains several patches that are not battery optimizations and several that violate the strict no-regression requirement. Do not use that bundle as evidence that a new battery patch is safe; test each candidate in isolation.

## Confidence-ranked recommendation

| Rank | Candidate | Expected scope | Performance risk | Functional risk | Recommendation |
|---|---|---:|---:|---:|---|
| A1 | Gate the initial disabled OnePlus task-tracker hrtimer | Device-specific, active/idle CPU timer activity | Very low while feature remains disabled | Low, but verify runtime enabling | Implement first, alone, after confirming the module is loaded and the proc flag is `0` |
| A2 | Stable hrtimer redundant-reprogram fix `ece8be21d8c93` | Generic timer core | Very low | Very low | Backport separately and measure; retain only if benefit exceeds noise |
| B1 | Fix RCU lazy hurry callbacks, then enable RCU lazy in a separate experiment | Kernel-wide RCU callback batching | Medium, especially tail latency | Medium | Do not enable by default until broad tests pass |
| B2 | Investigate unused OnePlus global jank-info enable flag and always-on hooks | Device-specific scheduler/cpufreq telemetry | Unknown | Medium to high due to OEM consumers | Trace and dependency-audit only; no patch yet |

If A1's module is not loaded on the tested ROM, or if `task_track_enable` is deliberately set to `1`, it cannot save energy and should not be applied as a battery change. If A2 is already present in the source used by a later manifest, it should not be duplicated.

## Exact source baseline

The audit used `configs/OP13-6.6.118.json`, whose relevant options are:

- model `OP13-6.6.118`
- SoC/device target `sun`
- Android 16, kernel 6.6
- DDK target `sun_perf`, chipset `peach-v2`
- `hmbird`, `opt`, `ddk`, and the monitor-injection path enabled

The pinned manifest is `manifests/a16/oneplus_13_6.6.118_w.xml`:

| Source tree | Exact revision |
|---|---|
| `OnePlusOSS/android_kernel_common_oneplus_sm8750` | `e1b346b6b4f4096eb342ae3684838a942fd6f6c4` |
| `OnePlusOSS/android_kernel_modules_and_devicetree_oneplus_sm8750` | `d50b305f7da9e14715a25120a4ac7b1a4b8b97c3` |
| `OnePlusOSS/android_kernel_oneplus_sm8750` | `6028f47faddaa27700f8dd3a1d83906ea8f27170` |

All three revisions are from the OnePlus 16.0.9.401 source synchronization dated 2026-07-22. The common-kernel commit says it is based on Qualcomm's `android15-6.6-2026-01_r22` release. At research time, the exact common revision was also the head of OnePlus's OnePlus 13 Android 16 branch, so this finding was not caused merely by the manifest lagging behind an available OnePlus 13 update.

The repository pins `WildKernels/kernel_patches` at `24865a0bc50dfb65b04153cc9ad2879a9c26cc7e`. Its optimization bundle is applied in `.github/actions/build-kernel/action.yml` when `opt` is true.

Stable comparison was performed through Linux `v6.6.151`, the latest 6.6 stable tag visible on 2026-08-15. A stable-version number alone is not sufficient for Android vendor kernels: the OnePlus tree includes Android and Qualcomm changes and can contain selected later fixes, so every candidate was checked against the exact pinned content.

## What "no performance impact" means

The only suitable class of change is one that removes demonstrably redundant work without changing a policy decision, deadline, frequency, voltage, residency threshold, queueing order, or resource limit.

For this handoff, accept a candidate only if all of these are true:

1. The targeted work is proven to occur on the real phone.
2. The targeted feature is disabled or the work is semantically redundant.
3. The change does not alter behavior when the feature is enabled.
4. Battery or power improvement exceeds measurement noise in repeated paired tests.
5. Performance and functional non-inferiority gates pass.

The absence of an obvious benchmark regression is not proof. A patch can improve an average score while worsening app-launch tails, frame deadlines, radio reconnect latency, fsync durability, suspend reliability, or thermal behavior.

## Candidate A1: stop the disabled OnePlus task-tracker timer

### Confirmed source behavior

The OnePlus `sun_perf` configuration has:

```text
CONFIG_OPLUS_FEATURE_CPU_JANKINFO=m
```

The SM8750 module build list includes `oplus_bsp_schedinfo.ko`, and `modules.list.msm.sun` includes it in the sun module set. This proves that the module is built and packaged; the real device must still be checked to prove it is loaded.

The relevant source is:

```text
vendor/oplus/kernel/cpu/sched/sched_info/osi_tasktrack.c
```

At the exact pinned revision:

- `tasktrack_enable` is zero-initialized unless the debug-only build condition is active.
- `tasktrack_init()` initializes a `CLOCK_MONOTONIC`, relative hrtimer.
- `tasktrack_init()` then calls `tasktrack_timer_start()` unconditionally.
- `tasktrack_timer_handler()` calls `update_tasktrack_time_win()`, forwards the timer, and returns `HRTIMER_RESTART`.
- `update_tasktrack_time_win()` immediately returns when `tasktrack_enable == 0`.
- Writing the proc enable node already starts the timer on enable and cancels it on disable.

The source calls this a 128 ms window. The actual constant is `1 << (7 + 20)` ns, or 134.217728 ms, which is approximately 7.45 timer expirations per second. An hrtimer expiration is not automatically proof of a full system resume, especially during suspend, but while the application processors are awake it can shorten idle intervals and require callback execution. Trace it before claiming a power saving.

### Minimal proposed change, not applied

The first implementation should change only the unconditional initial start:

```c
jank_update_task_status = jankinfo_update_task_status_cb;

if (READ_ONCE(tasktrack_enable))
	tasktrack_timer_start();
```

Using a plain `if (tasktrack_enable)` would also match the current code's synchronization style. `READ_ONCE` makes the intent explicit, but the implementing agent should confirm the existing headers expose it without adding unnecessary dependencies.

Do not initially rewrite the callback, remove the module, unregister all its hooks, or gate unrelated sched-info functions. The proc write handler already owns runtime start/stop behavior. Keeping the patch to the init call makes the intended equivalence easy to review:

- disabled before patch: callback fires and performs no tracking work;
- disabled after patch: callback does not fire;
- enabled through proc before/after patch: timer starts and tracking works;
- disabled again through proc before/after patch: timer is cancelled and buffers are cleared.

### Preconditions on the phone

Run these read-only checks on the exact test build:

```sh
uname -a
cat /proc/cmdline
lsmod | grep -E '(^| )oplus_bsp_schedinfo( |$)'
cat /proc/jank_info/cpu_jank_info/task_track_enable
```

Possible interpretations:

- Module loaded and flag `0`: A1 is applicable.
- Module absent: the timer is not running; do not patch it for battery.
- Flag `1`: the tracker is in use; init gating alone will not save energy after userspace enables it.
- Proc path differs or is absent: locate it with `find /proc/jank_info -maxdepth 3 -type f` and verify the loaded module/version.

### Proof before and after the patch

Use tracing only to prove callback behavior, not to measure energy, because tracing perturbs the system. If tracefs and symbols are available:

```sh
mountpoint /sys/kernel/tracing || mount -t tracefs tracefs /sys/kernel/tracing
grep -w tasktrack_timer_handler /proc/kallsyms
grep -w hrtimer_expire_entry /sys/kernel/tracing/available_events
```

Then capture a short, controlled screen-on-idle trace using `timer:hrtimer_expire_entry` and filter for `tasktrack_timer_handler` if this kernel permits event filters. Expected result:

- baseline, flag `0`: roughly 7.45 handler expirations per second while the relevant clock is running;
- patched, flag `0`: zero;
- patched, after writing `1`: expirations resume and proc output remains functional;
- patched, after writing `0`: expirations stop.

The runtime toggle test is mandatory. It catches an init-order or ownership mistake even if the default-disabled test passes.

### Why this is the strongest candidate

This patch removes work only from a feature whose own enable flag says it is off. It does not alter CPU frequency, placement, idle state selection, timer deadlines for enabled users, or scheduler behavior. The likely battery saving is small, but its performance risk is lower than policy-tuning patches.

## Candidate A2: upstream hrtimer redundant-reprogram fix

### Patch identity

Use the stable backport or its upstream source:

- Linux 6.6 stable: [`ece8be21d8c932ab2267191ae2ed664c79c8b6da`](https://github.com/gregkh/linux/commit/ece8be21d8c932ab2267191ae2ed664c79c8b6da)
- Mainline source: [`d19ff16c11db38f3ee179d72751fb9b340174330`](https://github.com/torvalds/linux/commit/d19ff16c11db38f3ee179d72751fb9b340174330)
- Subject: `hrtimer: Avoid pointless reprogramming in __hrtimer_start_range_ns()`
- First present in the 6.6 stable series at `v6.6.141`

The patch adds a check after enqueueing an hrtimer. If the CPU is already executing the hrtimer interrupt, the interrupt will reevaluate timer bases and program the clock-event device, so the start path avoids doing that programming redundantly.

An unmodified stable patch passes `git apply --check` against the exact pinned OnePlus common tree. The relevant logic was not already present there at research time.

### Why it qualifies

This changes neither the requested expiry time nor which timer is queued. It removes a hardware-programming operation that will immediately be performed by the already-running timer interrupt. It is upstream-reviewed, mainline, and stable-backported rather than an anonymous tuning patch.

The expected phone-level saving may be below measurement noise. That is not a reason to invent a larger claim. Keep the patch only if timer-heavy microtraces show the avoided path and paired power tests show a repeatable benefit or if the project chooses to take it as a normal stable correctness/efficiency backport.

### Implementation rules

- Preserve the original authorship, commit message, `Signed-off-by` lines, and upstream commit reference.
- Apply it to the common kernel only.
- Do not also take the larger companion trace-noise change merely because it is nearby in stable history; it has a different purpose.
- Recheck the exact manifest revision at implementation time.
- Run a clean build and verify that the resulting source contains the check exactly once.

## Candidate B1: RCU lazy callbacks, only as a corrected experiment

### What is already present

The exact GKI defconfig contains:

```text
CONFIG_RCU_NOCB_CPU=y
CONFIG_RCU_LAZY=y
CONFIG_RCU_LAZY_DEFAULT_OFF=y
```

The code therefore supports lazy RCU callback batching but defaults it off. Its Kconfig explicitly requires all CPUs to be offloaded with `rcu_nocbs=all`. The runtime boot parameter is `rcutree.enable_rcu_lazy=1`.

Android's lazy-RCU change batches callbacks while lightly loaded or idle. Its commit reports a reduction of roughly 5–10% of the power attributable to RCU requests in that condition. That statement must not be rewritten as a 5–10% improvement in total phone battery life.

Primary source:

- Android common kernel lazy-RCU change: [`e0297c38a54d51304c722405823a5e029ab6a091`](https://android.googlesource.com/kernel/common/+/e0297c38a54d51304c722405823a5e029ab6a091)

### Blocking bug in the pinned source

The pinned OnePlus common source's `wake_nocb_gp_defer()` lazy path checks:

```c
rdp->nocb_defer_wakeup
```

where the corrected Android code checks:

```c
rdp_gp->nocb_defer_wakeup
```

Android documented that the incorrect object can allow hurry callbacks to be delayed by several seconds in Update Engine workloads. The fix is:

- [`d64d8b7dab32e1356e13ee75a26a6bd386d7cbc1`](https://android.googlesource.com/kernel/common/+/d64d8b7dab32e1356e13ee75a26a6bd386d7cbc1), `FROMGIT: rcu: Fix delayed execution of hurry callbacks`

The one-line fix is missing from the exact OnePlus pin. Linux 6.6.151 also does not by itself establish that this Android-specific lazy-RCU baseline has the fix, so do not infer safety from the upstream stable version number.

### Required sequence

Do not simply set the default to on. A defensible experiment is:

1. Backport/adapt the hurry-callback fix, preserving provenance.
2. Audit the pinned Android 6.6 tree for the expected `call_rcu_hurry()` conversions and later lazy-RCU fixes.
3. Confirm the actual boot command line includes `rcu_nocbs=all`; `CONFIG_RCU_NOCB_CPU_DEFAULT_ALL` is not enabled in the audited GKI defconfig.
4. Confirm the running value exposed by the read-only module parameter:

   ```sh
   cat /sys/module/rcutree/parameters/enable_rcu_lazy
   ```

5. Enable lazy RCU in a separate test build/boot configuration, never bundled with A1 or A2.
6. Test Update Engine/OTA, app install and removal, block-device teardown paths, memory pressure, suspend/resume, networking, and UI tail latency.

### Why this is not Tier A

Lazy callbacks intentionally change when asynchronous cleanup occurs. They can reduce wakeups but increase callback latency and temporarily retain memory or resources. The documented Update Engine regression demonstrates that subtle users can be missed. Even after the known fix, this is a kernel-wide policy change and must pass stricter tests than the two redundant-work patches.

## Candidate B2: investigate always-on OnePlus jank telemetry

The same `oplus_bsp_schedinfo.ko` registers scheduler-tick/accounting and cpufreq fast-switch vendor hooks unconditionally. The scheduler path calls `jankinfo_update_time_info()`, which takes a global spinlock to ensure one update per tick before maintaining telemetry. The cpufreq path timestamps transitions and updates duration tables.

The module exposes `/proc/jank_info/cpu_jank_info/enable`, backed by `cpu_jank_info_enable`, but a source-wide search of the exact sched-info module found that variable only in the proc read/write implementation. It does not gate the hooks or their accounting. This suggests unfinished or disconnected enable semantics, but it is not enough to patch safely:

- OEM services can consume the proc statistics while the flag remains zero.
- Other OnePlus modules can call exported helpers or depend on accumulated state.
- The scheduler-tick hook is hot-path code; a guard could improve performance, but stale or absent statistics could change OEM decisions elsewhere.
- Some task-tracker hooks also service UX-throttling functions unrelated to the timer.

Next-agent research should trace readers of `/proc/jank_info`, module symbol consumers, and Android services before proposing a guard. Do not remove `oplus_bsp_schedinfo.ko` wholesale and do not gate every callback using the apparently unused global flag without that audit.

## Device-specific cpuidle result

The `sun_perf` fragment builds `CONFIG_CPU_IDLE_GOV_QCOM_LPM=m`, and `sun.bzl` packages `qcom_lpm.ko`. Its governor registers as:

```c
static struct cpuidle_governor lpm_governor = {
	.name = "qcom-cpu-lpm",
	.rating = 50,
	/* ... */
};
```

That rating is higher than the generic menu and TEO governors. If the module loads normally, generic menu/TEO selection fixes do not control OnePlus 13 idle-state selection. This is why otherwise plausible stable commits such as the following are not recommendations for this phone without live contrary evidence:

- `dffe2522`, `cpuidle: governors: teo: Drop misguided target residency check`
- `72b2db83`, `cpuidle: governors: menu: Always check timers with tick stopped`

Always read the live governor first:

```sh
cat /sys/devices/system/cpu/cpuidle/current_governor_ro 2>/dev/null || \
cat /sys/devices/system/cpu/cpuidle/current_governor
lsmod | grep -E '(^| )qcom_lpm( |$)'
```

If it says `qcom-cpu-lpm`, generic TEO/menu tuning is irrelevant. If it does not, investigate why the expected Qualcomm module is not loaded before changing cpuidle code.

Do not edit device-tree idle-state `entry-latency-us`, `exit-latency-us`, `min-residency-us`, or Qualcomm LPM policy based on generic internet values. These are hardware/firmware-specific break-even and latency contracts. Linux's [CPUIdle documentation](https://www.kernel.org/doc/html/latest/driver-api/pm/cpuidle.html) explains why target residency and exit latency affect both energy and response time.

## Audit of the current `opt` bundle

This table classifies the patches already applied by this repository. It is not a request to change them in this research-only task. It tells the implementing agent which existing modifications can confound battery measurements and which ones should not be presented as safe battery work.

| Existing patch | Actual behavior | Battery/no-performance assessment |
|---|---|---|
| `optimized_mem_operations` | Replaces ARM64 memory routines | Performance implementation change; may reduce energy per operation but requires correctness and benchmark validation |
| `file_struct_8bytes_align` | Raises `struct file` alignment | Layout/performance change, not a battery policy |
| `reduce_cache_pressure` | Changes default VFS cache pressure 100 to 50 | Retains inode/dentry cache longer and changes reclaim behavior; can affect memory pressure and app performance |
| `mem_opt_prefetch` | Changes ARM64 memory utility prefetch/assembly | Performance change, not a no-impact battery patch |
| `optimise_memcmp` | Replaces ARM64 `memcmp` implementation | Performance change; validate correctness and workloads |
| `minimise_wakeup_time` | Replaces a two-second alarmtimer suspend retry with nearest-alarm timing | Screen-off/suspend behavior change; downstream-only in this comparison and requires alarm/suspend reliability tests |
| `int_sqrt` | Replaces integer square-root implementation | Performance optimization, not battery policy |
| `force_tcp_nodelay` | Would change TCP latency/batching | Skipped when BBR/BBRv3 are enabled in the target; would affect network behavior if used |
| `reduce_gc_thread_sleep_time` | F2FS urgent-GC sleep 500 ms to 50 ms | Can poll/wake up to ten times as often during urgent GC; not a battery optimization |
| `add_timeout_wakelocks_globally` | Turns indefinite legacy `/sys/power/wake_lock` holds into 500 ms wakeup events | High correctness risk: can suspend while a legitimate operation still needs a wakelock; reject for this goal |
| `f2fs_reduce_congestion` | Congestion retry wait 20 ms to 6 ms | I/O latency/retry policy change; can increase polling and affects performance |
| `reduce_freeze_timeout` | Reduces freezer timeout to one second and prevents runtime adjustment | Only changes failed/slow suspend behavior; raises suspend reliability risk with no normal-success battery gain |
| `clear_page_16bytes_align` | Aligns ARM64 clear-page code | Microarchitectural performance change, not battery policy |
| `add_limitation_scaling_min_freq` and rewrite | Adds cluster minimum-frequency caps/controls | Direct cpufreq policy; can cap boosts or change responsiveness when used |
| `adjust_cpu_scan_order` | Changes scheduler idle-CPU scan start | Scheduler placement/performance behavior; benchmark rather than calling it free battery |
| `avoid_extra_s2idle_wake_attempts` | Coalesces repeated s2idle wake calls via `pm_abort_suspend` | Potentially useful only if the phone uses s2idle; subtle wake race/behavior change, and irrelevant if platform deep suspend is selected |
| `disable_cache_hot_buddy` | Disables a scheduler cache-locality feature | Can change migration, cache locality, energy, and performance; violates the strict constraint |
| `f2fs_enlarge_min_fsync_blocks` | Raises the F2FS minimum fsync block threshold 8 to 20 | Storage batching/latency/durability policy, not no-impact battery work |
| `increase_ext4_default_commit_age` | Extends journal commit age 5 s to 30 s | Expands the data-loss/durability window; reject as a battery optimization |
| `increase_sk_mem_packets` | Raises per-socket memory accounting quantum/default | Network/memory/performance change, not battery work |
| `reduce_pci_pme_wakeups` | Slows PME polling from 1 s to 4 s | Generic PCI workaround with no demonstrated SM8750 target and up to 3 s extra PME detection delay |
| `silence_irq_cpu_logspam` | Downgrades an IRQ-affinity warning | Can reduce pathological log load but hides diagnostics; fix/rate-limit the producer if it actually storms |
| `silence_system_logspam` | Drops `/dev/kmsg` messages containing selected process names | Hides diagnostics and changes logging semantics; not a sound battery patch |
| `use_unlikely_wrap_cpufreq` | Branch-layout hint in the added cpufreq limit code | Micro-performance change tied to non-upstream cpufreq behavior, not battery policy |

Two existing patches deserve explicit live checks:

```sh
cat /sys/power/mem_sleep
cat /sys/power/pm_freeze_timeout 2>/dev/null
```

If `mem_sleep` selects platform deep sleep rather than `s2idle`, the s2idle wake patch should not be credited for battery. A one-second freezer timeout can make a device appear to avoid wake drain by failing suspend attempts quickly while actually reducing time spent suspended; inspect suspend success counts, not just current draw during successful samples.

## Other ideas reviewed and rejected

### Globally enable power-efficient workqueues

The GKI defconfig audited here does not explicitly enable `CONFIG_WQ_POWER_EFFICIENT_DEFAULT`; a separate Qualcomm `defconfig` does, which is not proof of the final merged `sun_perf` configuration. Extract the built `.config` before drawing a conclusion.

Even when disabled, turning power-efficient workqueues on globally is not guaranteed performance-neutral. It can trade CPU locality and concurrency for fewer active CPUs. Linux's [workqueue documentation](https://www.kernel.org/doc/html/v6.6/core-api/workqueue.html) treats `WQ_POWER_EFFICIENT` as a policy choice, not a free optimization. Reject a blanket enable for the strict goal.

### Timer slack or converting timers to delayed work

Timer coalescing can save wakeups, but increasing timer slack or replacing exact timers without understanding their contract changes latency. Apply slack only to a specific, proven non-urgent periodic diagnostic task whose owner accepts delayed execution. The kernel's [delay/sleep guidance](https://www.kernel.org/doc/html/v6.6/timers/timers-howto.html) is a starting point, not permission to mechanically rewrite timers.

### Disabling debug and telemetry modules by name

The project already blacklists several logging/debug modules, but module names are not proof of runtime cost. Examples from the audit:

- the shipped OnePlus subsystem sleep monitor primarily reads shared-memory statistics on proc access rather than running an obvious periodic timer;
- OSML creates a kernel thread, but with its default disabled state the thread schedules indefinitely rather than polling periodically;
- a power diagnostic source contains periodic work but was not found in the sun packaged/load lists inspected.

Only remove a module after proving it is loaded, proving periodic/hot-path work, and auditing every symbol and userspace dependency. Otherwise the likely result is lost observability or a broken OEM service, not measurable battery gain.

### CPU, GPU, bus, thermal, and scheduler tuning

Undervolting, underclocking, reducing boost, relaxing scheduler placement, lowering bus votes, changing GPU idle thresholds, or raising thermal limits all violate the user's condition. "Same benchmark score" can hide worse interaction latency or more race-to-idle time. Conversely, higher clocks can sometimes finish work sooner. These policies must be evaluated as full energy/performance curves and are out of scope here.

### Storage batching

Longer journal commit intervals and broader fsync batching can reduce storage activity but change durability and I/O latency. A phone losing less data after reset is part of correct behavior. Do not treat data-loss exposure as a performance-neutral battery optimization.

### Radio and connectivity timers

Wi-Fi DTIM/listen interval, scan cadence, PCIe link power management, modem DRX, Bluetooth sniff intervals, and firmware autosuspend settings can dominate battery, but they also affect notifications, calls, throughput, reconnect time, and coexistence. Qualcomm firmware and Android HAL policy own much of this behavior. Do not change such values without firmware-specific documentation and controlled radio testing.

### Charging and battery-driver constants

Do not change charge current, voltage, temperature limits, gauge smoothing, watchdogs, or charger polling intervals to make discharge charts look better. These are safety, longevity, and reporting controls, not kernel-consumption optimizations.

## Upstream 6.6.118 to 6.6.151 comparison

Relevant timer, power, idle, RCU, and workqueue commits were reviewed rather than bulk-merging stable. Most are correctness fixes for paths not shown to consume normal OnePlus 13 power, architecture-specific fixes, removal/error-path races, or policy changes.

Notable results:

- `ece8be21` is the only clear semantics-preserving redundant-work backport recommended here.
- `a143c367`, `PM: runtime: Do not clear needs_force_resume with enabled runtime PM`, is already represented in the exact OnePlus source.
- generic TEO/menu fixes are not normally active under `qcom-cpu-lpm`.
- runtime-PM removal races and UFS suspend crash fixes may be valuable correctness backports, but should not be advertised as battery patches without a matching bug.
- stable 6.6.151 still has the upstream alarmtimer two-second suspend-retry behavior and unconditional wake path that the WildKernels patches alter, confirming those are downstream policy changes rather than stable fixes.

This does not mean all later stable fixes should be ignored. Security and correctness backporting is a separate maintenance task with different acceptance criteria.

## Required on-device baseline capture

Before any implementation, archive the following from the same ROM and kernel that will be used for A/B tests:

```sh
uname -a
cat /proc/version
cat /proc/cmdline
zcat /proc/config.gz > /data/local/tmp/running-kernel.config
cat /sys/power/mem_sleep
cat /sys/devices/system/cpu/cpuidle/current_governor_ro 2>/dev/null || true
cat /sys/module/rcutree/parameters/enable_rcu_lazy 2>/dev/null || true
lsmod
cat /proc/jank_info/cpu_jank_info/task_track_enable 2>/dev/null || true
cat /proc/jank_info/cpu_jank_info/enable 2>/dev/null || true
```

Also capture idle and suspend counters before and after each idle test. Paths vary across Qualcomm releases, so discover rather than assume:

```sh
find /sys/devices/system/cpu/cpuidle -maxdepth 3 -type f -print
find /sys/power -maxdepth 3 -type f \( -iname '*suspend*' -o -iname '*wakeup*' -o -iname '*sleep*' \) -print
find /sys/kernel/debug -maxdepth 3 -type f \( -iname '*rpmh*' -o -iname '*sleep*' -o -iname '*wakeup*' \) -print 2>/dev/null
```

Common useful counters, when present, include:

- per-CPU `cpuidle/state*/usage`, `time`, and `rejected`;
- `/sys/kernel/debug/wakeup_sources`;
- `/sys/power/suspend_stats/*` or an equivalent suspend-stat node;
- Qualcomm RPMh/master/subsystem sleep statistics;
- last wake reason;
- PowerStats HAL energy consumers and residency data.

Do not compare raw cumulative counters without recording uptime and taking deltas over equal-duration windows.

## Measurement protocol

### Instrumentation

Use an external power monitor or battery-replacement fixture where possible. Android's official [component power measurement guidance](https://source.android.com/docs/core/power/component) recommends average current at nominal voltage from an external monitor and warns that a connected USB host changes the power state. Disconnect USB during energy runs.

PowerStats/Perfetto can help attribute changes but are secondary to external energy measurement:

- [Android PowerStats HAL](https://source.android.com/docs/core/power/power-stats-hal)
- [Perfetto battery and power counters](https://perfetto.dev/docs/data-sources/battery-counters)

Fuel-gauge `current_now` and battery percentage are too noisy for small kernel changes unless tests are long and repeated. Never accept a patch based on one overnight percentage comparison.

### Experimental design

For each candidate:

1. Produce A and B builds from the same commit, config, toolchain, ROM, module set, and root configuration. The candidate must be the only source difference.
2. Randomize or alternate test order (`A/B/B/A`, for example) to reduce drift from aging, network conditions, and lab temperature.
3. Start in a narrow state-of-charge and battery-temperature range. Let the device settle after boot and after workload setup.
4. Perform at least five paired runs; ten is preferable for small idle changes.
5. Report joules or average watts over equal-duration windows, not just percentages.
6. Report distributions and 95% confidence intervals, not only means.
7. Record suspend success, subsystem residency, wake reasons, and thermals beside energy.

Suggested screen-off scenarios should be separated because radios dominate variance:

- airplane mode, all radios off;
- airplane mode with Wi-Fi connected to a quiet controlled AP;
- normal cellular idle under stable signal;
- AOD off and AOD on as separate tests;
- a notification/alarm scenario to catch delayed delivery.

Use six to eight hours for low-amplitude screen-off tests unless external instrumentation shows that a shorter interval has enough signal. Keep AP traffic and cellular signal conditions stable.

### Battery acceptance gate

A reasonable initial gate is:

- improvement greater than the established rig/run-to-run noise, preferably at least 3% for the selected scenario;
- paired 95% confidence interval excludes zero regression;
- no reduction in successful suspend time or deep subsystem residency;
- no new periodic wake source, alarm miss, or resume failure.

The 3% value is a project screening threshold, not a universal scientific constant. If the rig's coefficient of variation is larger, improve the experiment instead of lowering the bar.

### Performance non-inferiority gate

Use representative phone workloads, not only synthetic CPU throughput:

- app cold and warm launch p50/p95/p99;
- UI frame duration/jank p95/p99 at the same display mode;
- browser workload;
- short burst and sustained CPU tests;
- sustained GPU/game frame rate, frame pacing, power, and skin temperature;
- storage sequential/random I/O plus fsync latency;
- Wi-Fi and cellular throughput/latency under a controlled setup;
- camera launch/recording and media playback;
- memory-pressure/app-reload behavior.

Predeclare non-inferiority margins. A useful starting point is no more than 1% throughput loss and no more than 2% degradation in p95/p99 latency or jank outside normal test noise. Tighten or relax only from measured baseline variability, not after seeing an unfavorable result.

### Functional gate

At minimum:

- 100 screen-off suspend/resume cycles;
- RTC/alarm wake and scheduled notifications;
- incoming call and message during idle;
- Wi-Fi roam/reconnect and Bluetooth audio;
- camera, location, sensors, and fingerprint unlock;
- charging at normal and warm temperatures without changing safety policy;
- app install/uninstall and heavy package churn;
- Update Engine/OTA or an equivalent update-engine stress path for RCU lazy;
- memory pressure and repeated process death/reclaim;
- filesystem integrity check after controlled reboot tests;
- no new warnings, stalls, watchdogs, or subsystem restarts in kernel logs.

## Per-candidate test matrix

### A1 task-tracker timer

- Prove baseline handler cadence while flag is `0`.
- Prove zero cadence after patch while flag is `0`.
- Write `1`, verify handler cadence and every task-track proc read used by the ROM.
- Write `0`, verify cancellation and clean state.
- Compare screen-on idle and screen-off/AP-awake idle power.
- Confirm no change in app launch or frame pacing.

### A2 hrtimer reprogram avoidance

- Verify source contains the stable change exactly once.
- Run kernel timer selftests available in the build/test environment.
- Exercise high-resolution timers, alarm wake, media playback, input, and suspend/resume.
- Measure a timer-heavy controlled workload and normal idle; expect small effect.
- Do not treat fewer trace records from an unrelated companion patch as energy proof.

### B1 RCU lazy

- Verify the hurry-callback fix in the built source.
- Verify `rcu_nocbs=all` and lazy parameter state at runtime.
- Run relevant RCU selftests/rcutorture in a suitable test build if feasible.
- Stress Update Engine, loop/dm/block setup and teardown, app installs, network namespace/device churn, and memory pressure.
- Track callback backlog, slab/memory growth, reclaim stalls, and p95/p99 latency.
- Test enable and disable builds independently; do not mix with A1/A2.

## Proposed implementation order for the next agent

1. Re-resolve the manifest pins and confirm that this report's source lines still match.
2. Download a successful baseline artifact and preserve its exact `.config`, module list, command line, and build metadata.
3. Run the A1 phone preconditions. If applicable, prepare one small vendor-module patch that gates only `tasktrack_timer_start()` in init.
4. Build and validate A1 alone. Do not combine it with the hrtimer patch.
5. Prepare A2 as a provenance-preserving upstream stable backport, build and validate it alone.
6. Compare A1, A2, and baseline using the same protocol. Only after individual proof test an A1+A2 build for interaction.
7. Treat RCU lazy as a distinct research branch. Backport the known fix before changing the boot default or parameter.
8. Do not edit the broad existing optimization bundle during the first experiment; record it as a shared baseline confounder. A later cleanup can evaluate those patches one by one.
9. Run `./validate_workflow.sh` after any eventual workflow/action change and trigger a live build only after local validation passes.

## Handoff checklist

- [ ] Confirm repository HEAD and exact source pins have not changed.
- [ ] Confirm `oplus_bsp_schedinfo.ko` is loaded.
- [ ] Confirm `task_track_enable` remains zero during ordinary use.
- [ ] Trace the baseline task-tracker timer cadence.
- [ ] Extract the actual final kernel `.config`; do not infer it solely from fragments.
- [ ] Record live cpuidle governor and suspend mode.
- [ ] Check whether stable hrtimer commit `ece8be21` is still missing and cleanly applicable.
- [ ] Keep A1 and A2 in separate commits/builds.
- [ ] Preserve upstream provenance for A2 and the RCU fix.
- [ ] Define battery and performance thresholds before testing.
- [ ] Use external power measurement when possible and disconnect USB during runs.
- [ ] Do not enable lazy RCU without `rcu_nocbs=all` and the hurry-callback fix.
- [ ] Do not claim total-phone battery percentages from subsystem-specific upstream claims.
- [ ] Reject candidates whose measured benefit is within noise.

## Primary source links

### Exact OnePlus sources

- [OnePlus common kernel at `e1b346b6`](https://github.com/OnePlusOSS/android_kernel_common_oneplus_sm8750/tree/e1b346b6b4f4096eb342ae3684838a942fd6f6c4)
- [OnePlus modules/device tree at `d50b305f`](https://github.com/OnePlusOSS/android_kernel_modules_and_devicetree_oneplus_sm8750/tree/d50b305f7da9e14715a25120a4ac7b1a4b8b97c3)
- [`osi_tasktrack.c` at the exact pin](https://github.com/OnePlusOSS/android_kernel_modules_and_devicetree_oneplus_sm8750/blob/d50b305f7da9e14715a25120a4ac7b1a4b8b97c3/vendor/oplus/kernel/cpu/sched/sched_info/osi_tasktrack.c)
- [`osi_base.h` timer-window constants](https://github.com/OnePlusOSS/android_kernel_modules_and_devicetree_oneplus_sm8750/blob/d50b305f7da9e14715a25120a4ac7b1a4b8b97c3/vendor/oplus/kernel/cpu/sched/sched_info/osi_base.h)
- [`oplus_sched_info.c` always-registered hooks](https://github.com/OnePlusOSS/android_kernel_modules_and_devicetree_oneplus_sm8750/blob/d50b305f7da9e14715a25120a4ac7b1a4b8b97c3/vendor/oplus/kernel/cpu/sched/sched_info/oplus_sched_info.c)
- [`osi_enable.c` global enable proc node](https://github.com/OnePlusOSS/android_kernel_modules_and_devicetree_oneplus_sm8750/blob/d50b305f7da9e14715a25120a4ac7b1a4b8b97c3/vendor/oplus/kernel/cpu/sched/sched_info/osi_enable.c)
- [OnePlus MSM kernel at `6028f47f`](https://github.com/OnePlusOSS/android_kernel_oneplus_sm8750/tree/6028f47faddaa27700f8dd3a1d83906ea8f27170)
- [`sun_perf.config` at the exact pin](https://github.com/OnePlusOSS/android_kernel_oneplus_sm8750/blob/6028f47faddaa27700f8dd3a1d83906ea8f27170/arch/arm64/configs/vendor/sun_perf.config)
- [`qcom-lpm.c` at the exact pin](https://github.com/OnePlusOSS/android_kernel_oneplus_sm8750/blob/6028f47faddaa27700f8dd3a1d83906ea8f27170/drivers/cpuidle/governors/qcom-lpm.c)
- [`sun.bzl` packaged modules](https://github.com/OnePlusOSS/android_kernel_oneplus_sm8750/blob/6028f47faddaa27700f8dd3a1d83906ea8f27170/sun.bzl)
- [`modules.list.msm.sun`](https://github.com/OnePlusOSS/android_kernel_oneplus_sm8750/blob/6028f47faddaa27700f8dd3a1d83906ea8f27170/modules.list.msm.sun)
- [WildKernels patch repository at the pipeline pin](https://github.com/WildKernels/kernel_patches/tree/24865a0bc50dfb65b04153cc9ad2879a9c26cc7e)

### Upstream and Android kernel sources

- [Stable hrtimer redundant-reprogram fix](https://github.com/gregkh/linux/commit/ece8be21d8c932ab2267191ae2ed664c79c8b6da)
- [Mainline hrtimer source commit](https://github.com/torvalds/linux/commit/d19ff16c11db38f3ee179d72751fb9b340174330)
- [Android lazy-RCU implementation commit](https://android.googlesource.com/kernel/common/+/e0297c38a54d51304c722405823a5e029ab6a091)
- [Android hurry-callback delay fix](https://android.googlesource.com/kernel/common/+/d64d8b7dab32e1356e13ee75a26a6bd386d7cbc1)
- [Android common-kernel branch `android15-6.6`](https://android.googlesource.com/kernel/common/+/refs/heads/android15-6.6)
- [Linux CPUIdle documentation](https://www.kernel.org/doc/html/latest/driver-api/pm/cpuidle.html)
- [Linux 6.6 workqueue documentation](https://www.kernel.org/doc/html/v6.6/core-api/workqueue.html)
- [Linux 6.6 timer delay/sleep guidance](https://www.kernel.org/doc/html/v6.6/timers/timers-howto.html)

### Android measurement sources

- [Android component power measurement](https://source.android.com/docs/core/power/component)
- [Android PowerStats HAL](https://source.android.com/docs/core/power/power-stats-hal)
- [Perfetto power data sources](https://perfetto.dev/docs/data-sources/battery-counters)

## Final decision rule

The next agent should begin with A1 because it removes an objectively pointless periodic timer when its feature is off. A2 is a sound, very small upstream efficiency backport. Neither should be advertised as extending battery life until the OnePlus 13 measurements show a repeatable improvement. RCU lazy is potentially more meaningful, but it deliberately changes callback timing and therefore does not satisfy the strict requirement without a much larger validation campaign.
