# Troubleshooting

Start with the failed stage, not the last line of the job. Re-run with
`debug=true`, download the
`debug-<base>-<root>-<profile>-<target>-<optimization>-<lto>-<run-id>`
artifact, and preserve the original run ID.

## Source synchronization fails

- Confirm the selected base exists in `configs/profiles/` and its
  `locked_manifest` exists under `manifests/lockfiles/`.
- Check that every project in the locked XML has a full revision SHA. A moving
  OnePlus branch in the upstream manifest is provenance, not a checkout input.
- Compare the failing project URL with `out/source/resolved-manifest.xml` and
  the dependency lock. Do not fix the job by changing a commit to `main`,
  `master`, `dev`, or another branch name.
- For a download, recalculate the file SHA-256 from a trusted upstream release
  and update the lock in a reviewed change if upstream intentionally changed
  the asset.
- Hosted jobs write `source-sync.log` and one-minute
  `source-sync-telemetry.log` snapshots with load, RAM, disk, and top resident
  processes. A GitHub annotation that the runner lost communication without a
  finalized log or artifact is infrastructure loss rather than a surfaced
  manifest error; rerun it once before changing a lock. Hosted checkout uses
  two workers while repo network fetches remain serial.

## A patch fails or is already applied

Inspect the patch log and every `.rej`/`.orig` in the debug bundle. Confirm the
base, KMI, root variant, dependency commit, and patch-series order. Patch
failures are not warnings: update or condition the patch and make all three
base rehearsals pass on disposable locked-source checkouts.

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

A compiler redefinition of `scaling_min_freq_limit` in
`drivers/cpufreq/cpufreq.c` means the local minimum-limit hook reused the
sysfs attribute identifier for its per-CPU backing array. The locked patch
keeps the public `scaling_min_freq_limit` sysfs name and uses
`scaling_min_freq_limit_store` only for backing storage. This compatibility
fix applies to both common and MSM kernel trees and is independent of the
KernelSU/SUSFS driver integration.

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

## Modules-only build cannot find a kernel artifact

A manual modules-only Actions run resolves the newest unexpired matching kernel
artifact. To select a specific run, set the repository variable
`KERNEL_ARTIFACT_RUN_ID`. The referenced run must use the same base, root
variant, feature profile, optimization, LTO mode, branding, and current commit
SHA and still retain the reusable
`kernel-build-<base>-<root>-<profile>-mixed-<optimization>-<lto>-<branding>`
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
