# Troubleshooting

Start with the failed stage, not the last line of the job. Re-run with
`debug=true`, download the
`debug-<base>-<root>-<profile>-<target>-<optimization>-<lto>-<run-id>-attempt-<n>`
artifact, and preserve the original run ID.

## Source synchronization fails

- Confirm the selected base exists in `configs/profiles/` and its
  `locked_manifest` exists under `manifests/lockfiles/`.
- Check that every project in the locked XML has a full revision SHA. A moving
  OnePlus branch in the upstream manifest is provenance, not a checkout input.
- Compare the failing project URL with
  `out/source/.op13/<base>-manifest-resolved.xml` and
  the dependency lock. Do not fix the job by changing a commit to `main`,
  `master`, `dev`, or another branch name.
- For a download, recalculate the file SHA-256 from a trusted upstream release
  and update the lock in a reviewed change if upstream intentionally changed
  the asset.
- Hosted jobs write `source-sync.log` and one-minute
  `source-sync-telemetry.log` snapshots with load, RAM, disk, and top resident
  processes. The live job log also receives a one-minute
  `[source-sync heartbeat]` line containing elapsed time, load, and remaining
  workspace bytes. A GitHub annotation that the runner lost communication without a
  finalized log or artifact is infrastructure loss rather than a surfaced
  manifest error; rerun it once before changing a lock. Hosted checkout uses
  two workers while repo network fetches remain serial.

## A patch fails or is already applied

Inspect `patch-operations.json` in the debug bundle. It is written after every
operation; a required failure records the exact operation, kernel tree, and
bounded command output before the patch command exits. Any `.rej`/`.orig`
bytes are copied under `patch-residue/files/` and bound by
`patch-residue/PATCH-RESIDUE-MANIFEST.json`. Confirm the base, KMI, root
variant, dependency commit, and patch-series order. Patch failures are not
warnings: update or condition the patch and make both OOS 15 rehearsals plus
the OOS 16 full compile pass on disposable locked-source checkouts.

An unexpected reverse check ("already applied") also needs investigation; it
can indicate that the official source incorporated the change or that two
series overlap.

## Requested Kconfig symbol is missing

Compare the requested fragment, final `out/build/.config`, and Kconfig
dependency chain. The pipeline runs `olddefconfig`, so a symbol can disappear
when its dependencies are unavailable on arm64/SM8750. Add the real dependency
or correct the feature implementation; do not remove the final assertion while
still advertising the feature.

For O2/O3 or Thin/Full LTO, confirm the workflow override was applied after the
base fragments. For `root=none`, root and SUSFS symbols are intentionally
excluded from verification.

## Compiler or Rust failure

Use only the toolchain revisions provided by the resolved OnePlus manifest or
the dependency lock. Record `clang --version`, `rustc --version`, and bindgen
version from the debug log. Rust Binder is disabled in every shipped profile
because the pinned SM8750 sources contain no implementation. An unknown Rust
Binder symbol disappearing during `olddefconfig` is a hard configuration error,
not evidence that the feature built.

For OOS15 China HMBIRD errors in `sched_assist/sa_common.c` mentioning a
missing `oplus_task_struct.scx`, `SCX_SLICE_DFL`, or `scx_task_stats`, inspect
`.op13/oos15-cn-hmbird-overlay.json` in the source/patch diagnostics. The
repository-owned CN compatibility gate accepts only the locked module commit,
blob, executable mode, LF-only full-file preimage, and exact clean postimage.
Do not restore deprecated sched_ext storage or weaken the preimage gate; an
unexpected digest means the module lock and compatibility contract must be
reviewed together.

For an OOS15 error from `include/linux/sched/sched_ext.h` reporting that
`oplus_task_struct` has no member named `scx`, inspect
`.op13/oos15-hmbird-sched-prop.json`. The CN and Global Fengchi inputs create
the same stale compatibility header in both kernel trees. The locked transform
preserves each helper's null/error behavior while moving only its three
`sched_prop` accesses to `hmbird_entity`. Missing or changed preimage evidence
must be reviewed with the pinned Fengchi patch instead of restoring SCX
storage. This failure is independent of KernelSU/SUSFS integration.

A compiler redefinition of `scaling_min_freq_limit` in
`drivers/cpufreq/cpufreq.c` means the local minimum-limit hook reused the
sysfs attribute identifier for its per-CPU backing array. The locked patch
keeps the public `scaling_min_freq_limit` sysfs name and uses
`scaling_min_freq_limit_store` only for backing storage. This compatibility
fix applies to both common and MSM kernel trees and is independent of the
KernelSU/SUSFS driver integration.

An exit status `126` or `Permission denied` from
`drivers/of/overwriter/overwrite_configs/convert_configs.sh` means the pinned
HMBIRD overwriter's generated helper was not executable. Inspect
`.op13/hmbird-overwriter-mode.json`. The post-patch gate verifies the exact
canonical WildKernels patch and both exact 3,528-byte converter copies before
changing only their modes from `0644` to `0755`; it rolls both trees back if
the transaction or stamp fails. A changed patch, converter digest, or starting
mode requires a lock review rather than bypassing the gate. This failure is
independent of KernelSU/SUSFS integration.

If Kleaf reports that 38 `drivers/media/tuners/*.ko` files were built but not
copied, do not disable SDR or add ad hoc `module_outs` in a synced checkout.
The locked OnePlus tuner Kconfig defaults 37 symbols to `m` when media
subdriver autoselection is off, and `CONFIG_MEDIA_TUNER_SIMPLE` emits two
files. The repository-owned final-config mapper must resolve that exact
37-symbol/38-output closure into `OP13_MODULE_IMPLICIT_OUTS` for each locked
common target; a different list means the source/config locks need review.

## Disk or memory exhaustion

For Actions, read `disk-cleanup.txt`, `disk-layout.txt`,
`disk-after-lvm.txt`, `disk-before.txt`, `disk-after.txt`, and
`output-sizes.txt`. If setup fails before checkout, use the
`pre-lvm-<base>-<root>-<run-id>` artifact together with the step log. The job
stops before source synchronization unless its
workspace is the expected two-PV LVM mount with at least 100 GiB available and
the runner root retains at least 8 GiB. `preparation.properties` and
`disk-layout.txt` record whether the runner used `dual` or `shared` storage;
both modes keep two distinct loop devices and are checked against different
backing-file placement and reserve contracts. For a local build, use a clean
workspace, lower source-sync/build parallelism, or place the repository on a
larger case-sensitive Linux filesystem. Do not delete source projects in the
middle of a build, because modules and DTBs must remain tied to the same
manifest.

During the kernel phase, `kernel-build-telemetry.log` records an immediate,
one-minute, and final UTC snapshot with elapsed time, load, `MemAvailable`,
swap, workspace capacity, and the highest-RSS processes. The live log receives
matching `[kernel-build heartbeat]` summaries. A single lost-runner annotation
without a final log or artifact is not enough evidence to impose Bazel job or
memory limits; rerun once and compare the telemetry first. Debug upload omits
only the disposable `.op13/config-work` source copies while retaining the
build context, configuration records, kernel log, final `.config`, `vmlinux`,
`System.map`, `Module.symvers`, and modules.

After a repeated OOS 16 compile-phase runner loss, keep the sealed hosted policy
at two Bazel jobs, two local CPU resources, 6144 MiB of schedulable RAM, and an
8-GiB swap volume. If another runner disappears, compare the last surviving
heartbeat when available before reducing concurrency to one; a one-job clean
compile can approach the six-hour hosted-job ceiling.

## Strict GKI symbol-list failure

If the final Bazel actions report `from_kuid` for
`oplus_bsp_mm_osvelte.ko` or `from_kuid_munged` for `msm_sysstats.ko`, inspect
`kernel_platform/common/.op13-kmi-symbol-exports.json` first. The repository
integrates those two vendor-module requirements through exact per-base
pre/postimage contracts; do not disable strict mode or append an unbounded
symbol set. A missing stamp means the common patch series did not complete. A
pre/postimage error means the locked OnePlus source changed and requires a
reviewed contract update rather than a retry.

## Modules-only build cannot find a kernel artifact

A manual modules-only Actions run resolves the newest unexpired matching kernel
artifact. To select a specific run, set the repository variable
`KERNEL_ARTIFACT_RUN_ID`. The referenced run must use the same base, root
variant, feature profile, optimization, LTO mode, branding, and current commit
SHA and still retain the reusable
`kernel-build-<base>-<root>-<profile>-mixed-<optimization>-<lto>-<branding>-timestamp-<key>`
artifact. A selected run ID does not bypass those checks. Start a new matching
mixed run when the artifact has expired.

Locally, verify that the kernel output contains `.config`, `Module.symvers`,
`System.map`, kernel release metadata, and built-in module metadata before
running `build-modules.sh`.

## Module vermagic or symbol failure

```bash
uname -r
modinfo -F vermagic path/to/module.ko
modprobe --dry-run MODULE_NAME
dmesg -T | tail -n 200
```

A release mismatch, `invalid module format`, unknown symbol, failed `depmod`, or
different `Module.symvers` means the module bundle does not match the installed
kernel. Rebuild kernel and modules from the same resolved manifest and context;
do not force-load the module.

## Device does not boot

Confirm that the artifact targets the installed OxygenOS profile and that its
checksum passed. Disconnect optional USB hardware and try the documented stock
rollback. Preserve available recovery/bootloader logs and report the exact OTA,
artifact SHA-256, run ID, and last known boot stage.

For an OOS 16 SUSFS failure, reproduce without SUSFS using `root=none` to
separate base kernel compatibility from the experimental root/hiding path. This
does not establish that another profile or firmware is compatible.

## GitHub Actions diagnosis

```bash
gh run view RUN_ID --repo Hipuu/OnePlus13-KernelBuilder --log-failed
gh run watch RUN_ID --repo Hipuu/OnePlus13-KernelBuilder --exit-status
gh run rerun RUN_ID --repo Hipuu/OnePlus13-KernelBuilder --failed
```

Before filing an issue, attach the debug artifact or the smallest sanitized
files that demonstrate the problem. Remove account names, device serials,
tokens, keys, and unrelated log content.
