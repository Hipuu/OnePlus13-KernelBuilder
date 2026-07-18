# Building

The build is driven by three independent selectors:

- base: `oos15-cn`, `oos15-global`, or `oos16`;
- feature profile: `wild`, `nethunter`, or `full`;
- root: `kernelsu`, `kernelsu-next`, or `none`.

Build target, optimization, and LTO are separate choices. The default GitHub
Actions build is OOS 16, full features, KernelSU-Next, mixed output, O2, and
Thin LTO.

The pinned KernelSU and KernelSU-Next `kernel/include/uapi -> ../../uapi` Git
links are explicit source contracts. The staging helper verifies each exact
commit, link mode, blob, target, UAPI inventory, and clean checkout. It writes
the driver from immutable Git blobs and materializes the pinned UAPI headers
as regular files. Any other link or target stops the build; dependency links
are never copied through blindly.

After SUSFS integration, an exact preimage-checked transform pins the versions
that upstream would derive from full Git history: KernelSU `32525` and
KernelSU-Next dev `33207` / nearest tag `v3.2.0`. The final 91-file or 92-file
driver subtree must match its audited digest before it is installed into both
locked kernel trees.
For KernelSU-Next, the compatibility sequence is locked to the audited
`Hipuu/OnePlus_KernelSU_SUSFS` snapshot `7ea1d5058255fba3cf8e836d0c6c27c9546b7f6c`:
the SUSFS v2.2.0 base patch, the six exact reject repairs,
`overwrite_hook_mode.patch`, and then `ksu_toolkit.patch`. Every patch blob,
the reject inventory, the hook-mode result, and the final driver tree have
independent fingerprints, so a mismatched KernelSU/SUSFS pair fails before
configuration or compilation.

SUSFS v2.2.0 no longer defines the older `HAS_MAGIC_MOUNT`, automatic bind-
mount, `TRY_UMOUNT`, overlay, or `SUS_SU` Kconfig switches that the reference
workflow still appends. Those unknown names are deliberately omitted here;
`olddefconfig` would otherwise discard them while making the build appear to
enable features that are absent from the locked patch. The requested SUSFS
surface is the exact Kconfig surface produced by the audited v2.2.0 patch.
Configuration is resolved with the locked common tree's declared Clang
toolchain, and `scripts/config --keep-case` preserves mixed-case symbols such
as the MT76 USB drivers.

The currently audited root commits are KernelSU
`b0bc817b4e966aa6aa830834eaf6ef765d821d40`, KernelSU-Next
`1a0ef4898568a013b51d74ceb5593b83725bfb78`, and SUSFS
`a8c720c42ca46fca13179280b13aa13c9fbe1562`. A different requested commit
must be introduced as a lock update together with its source-tree, patch,
version, and final-driver fingerprints; it is not accepted as an unchecked
runtime override.

The locked OOS15 China scheduler overlay contains an older
`scx_sched_fork()` hook that addresses SCX storage removed by the selected
Fengchi/HMBIRD patch. Full and Wild China builds therefore run one CN-only
compatibility transform immediately after vendor HMBIRD integration. It
requires modules commit `a85bac41e21a790e216039cde1d34a6c5d6416d1`, executable
blob `625b526e0c234212152b46a0e5b874368f5a3902`, and full-file SHA-256
`96b1a2cfe793bc33f1e6c942058767587d95ff4317b8811a305855fd570123af`.
The postimage matches the fork-handler cleanup already present in the locked
Global and OOS16 module sources and is independently fingerprinted; no
KernelSU or SUSFS input is changed by this base-source repair.

## Reproducible inputs

`dependencies/lock.yml` pins every dependency. Each release profile points to
a repository-owned file under `manifests/lockfiles/`; every project revision in
those XML files is a full commit SHA. The original manifest URL, branch, file,
and pinned manifest-repository revision remain in the profile for provenance
and source monitoring.

New Git dependency checkouts force `core.autocrlf=false` and `core.eol=lf`
before materialization. Root-driver and patch consumers also read immutable
Git blobs directly, so their bytes do not depend on the runner's host settings
or an older restored worktree.

`scripts/sync-sources.sh` must initialize from the locked manifest. After sync,
the build records `repo manifest -r`, the lock file, and a build context. A
source-monitor run may report an upstream change, but it never edits the lock.

## GitHub Actions

From the Actions page, run **Build OnePlus 13 kernel** and select:

| Input | Values | Default |
| --- | --- | --- |
| `base` | `oos16`, `oos15-global`, `oos15-cn` | `oos16` |
| `root` | `kernelsu-next`, `kernelsu`, `none` | `kernelsu-next` |
| `kernelsu_commit` | optional lowercase 40-character lock assertion | locked selection |
| `susfs_commit` | optional lowercase 40-character lock assertion | locked SUSFS |
| `profile` | `full`, `wild`, `nethunter` | `full` |
| `target` | `kernel`, `modules`, `mixed` | `mixed` |
| `optimization` | `O2`, `O3` | `O2` |
| `lto` | `thin`, `full` | `thin` |
| `clean` | boolean | `true` |
| `cache` | boolean | `true` |
| `debug` | boolean | `false` |
| `pre_release` | boolean | `true` |

Cached Actions mode is `clean=false` and `cache=true`. Verified dependency
sources remain in `.cache/op13`. Kernel and mixed builds also restore the
device-declared `kernel_platform/bazel-cache` after locked source sync and save
it immediately after successful kernel compilation. Clean builds delete that
local Bazel cache and never restore or save it; modules-only builds do not use
a module-work cache and continue to consume a versioned kernel artifact.

The Bazel cache selector includes runner OS and architecture, base, root,
feature profile, optimization, and LTO. Its final key adds one canonical hash
over locked manifests, dependency locks, configs, schemas, patches, and root /
build scripts. A selector-scoped restore prefix can reuse individual Bazel
actions after one of those checked-in inputs changes, while Bazel still keys
the actions by their exact inputs. Debug `cache-statistics.txt` records the
requested and matched keys, pre/post sizes, threshold, eligibility, and save
outcome.

The uncompressed save threshold defaults to exactly `7516192768` bytes (7
GiB). Set the repository variable `OP13_COMPILE_CACHE_MAX_BYTES` to another
positive decimal byte count to tune it. An absent, empty, exact-hit, failed,
or oversized cache is not saved. Source trees, `Module.symvers`, and kernel
lineage metadata remain excluded from Actions cache storage.

The locked source graph occupies roughly 57–60 GiB on the hosted runner. Before
checkout in sync/build jobs, Actions removes only an explicit allowlist of
unused runner-image paths. The storage preflight accepts both hosted layouts:
the older separate `/` and `/mnt` filesystems and the newer consolidated
145-GiB root filesystem where `/mnt` is an ordinary directory. The
immutable-pinned disk action always creates two exact loop-backed LVM physical
volumes, recreates 4 GiB of swap, and mounts the ext4 build volume at the whole
GitHub workspace. Separate-device mode reserves 8.25 GiB on `/` and 1 GiB on
`/mnt`. Shared-device mode stages the action's sequential allocations with
9.25/8.25-GiB reserve inputs, producing a 1-GiB second PV while still leaving
8.25 GiB on the common filesystem. An exact active `/swapfile` from the
consolidated image is reclaimed before the LVM swap is created.

A fail-closed validator requires at least 8 GiB to remain on `/` and 100 GiB
to be available in the pooled workspace. It proves the declared topology,
mount, backing-file placement, distinct loop devices, two physical volumes,
logical volumes, active swap, write access, and canonical non-symlink paths
before source synchronization. Hosted jobs keep repo network fetches serial
(`--jobs-network 1`) and use two checkout workers, leaving capacity for the
runner agent and filesystem; the local CLI default remains four workers. A
60-second observer records load, memory, filesystem space, and the highest-RSS
processes beside the complete source-sync log in `out/debug`. This avoids
transient disk/resource exhaustion without changing a locked revision or the
canonical `out/source` layout.

Set optional repository variables `KERNEL_BRANDING` and `BUILD_TIMESTAMP` for
single-line build metadata. A manual modules-only build resolves the newest
unexpired matching kernel artifact. Set `KERNEL_ARTIFACT_RUN_ID` to a numeric
run ID to choose a specific matching artifact instead.

Dispatch from an authenticated GitHub CLI session:

```bash
gh workflow run build.yml --repo Hipuu/OnePlus13-KernelBuilder \
  -f base=oos15-global \
  -f root=kernelsu \
  -f kernelsu_commit=b0bc817b4e966aa6aa830834eaf6ef765d821d40 \
  -f susfs_commit=a8c720c42ca46fca13179280b13aa13c9fbe1562 \
  -f profile=wild \
  -f target=kernel \
  -f optimization=O2 \
  -f lto=thin \
  -f clean=true \
  -f cache=true \
  -f debug=true \
  -f pre_release=true
```

The two commit fields are assertions, not unchecked checkout overrides. A
blank value resolves to `dependencies/lock.yml`; a supplied value must be the
exact full commit for the selected root variant and locked SUSFS revision.
Any other value stops before source synchronization and requires a reviewed
lock plus compatibility-fingerprint update.

The `release.yml` workflow requires `tag`, then base, root, profile, target,
optimization, LTO, clean, debug, and pre-release inputs. Release rebuilds use
the checked-in dependency lock; the called build defaults cache on, while the
release default `clean=true` skips restoration. Publishing occurs only
through the `release` environment; configure its reviewer/protection rules in
the repository settings before publishing. A modules-only release creates a
matching kernel prerequisite in the same workflow run.

Nightly runs start daily at 18:17 UTC: OOS 16 full/KernelSU-Next O3 Full LTO,
OOS 15 global full/KernelSU-Next O2 Thin LTO, and an OOS 15 China full/KernelSU
O2 Thin LTO compatibility build. Nightlies use `clean=false` with cache enabled.
The source monitor runs daily at 19:43 UTC and reports upstream changes without
editing locks. Cleanup runs Sundays at 20:29 UTC, deleting caches by
`last_accessed_at` and artifacts by `created_at` after the configured 14-day
threshold.

## Local build

Use a recent Linux host. GitHub Actions uses Ubuntu 24.04. Source sync and
kernel compilation need a case-sensitive filesystem, substantial free disk
space, and enough memory for the selected job count. Windows users should use
a Linux VM or WSL filesystem rather than building from an NTFS-mounted tree.

Start with repository validation and inspect each command's help:

```bash
bash scripts/validate.sh
bash scripts/sync-sources.sh --help
bash scripts/apply-series.sh --help
bash scripts/configure.sh --help
```

A full local mixed build follows the same stages as Actions:

```bash
mkdir -p out/source out/build out/build/modules out/debug out/dist

KERNELSU_COMMIT='FULL_LOWERCASE_40_CHARACTER_KERNELSU_SHA'
SUSFS_COMMIT='FULL_LOWERCASE_40_CHARACTER_SUSFS_SHA'
python3 scripts/op13.py resolve-root-lock \
  --root kernelsu-next \
  --kernelsu-commit "$KERNELSU_COMMIT" \
  --susfs-commit "$SUSFS_COMMIT" \
  > out/debug/root-selection.json

bash scripts/sync-sources.sh \
  --base oos16 \
  --output out/source

bash scripts/apply-series.sh \
  --base oos16 \
  --profile full \
  --root kernelsu-next \
  --source-dir out/source \
  --log out/debug

bash scripts/configure.sh \
  --base oos16 \
  --profile full \
  --root kernelsu-next \
  --optimization O2 \
  --lto thin \
  --build-target mixed \
  --source-dir out/source \
  --output out/build

bash scripts/build-kernel.sh \
  --source-dir out/source \
  --output out/build \
  --clean \
  --debug

bash scripts/build-modules.sh \
  --source-dir out/source \
  --kernel-output out/build \
  --output out/build/modules \
  --clean \
  --debug

bash scripts/verify.sh \
  --base oos16 \
  --profile full \
  --root kernelsu-next \
  --build-target mixed \
  --output out/build

bash scripts/package.sh \
  --base oos16 \
  --profile full \
  --root kernelsu-next \
  --build-target mixed \
  --input out/build \
  --output out/dist \
  --debug \
  --pre-release
```

Set optional branding before configuration/build:

```bash
export KERNEL_BRANDING='OnePlus13-KernelBuilder'
export BUILD_TIMESTAMP='2026-07-14T12:00:00Z'
```

`BUILD_TIMESTAMP` accepts a timezone-qualified RFC3339 value or an epoch
integer. When it is unset, `SOURCE_DATE_EPOCH` accepts an epoch integer; when
both are unset, the builder uses the locked common-kernel commit timestamp.
The resolved value is exported as `SOURCE_DATE_EPOCH` and drives Kbuild's
timestamp, fake-config rebuild marker, and deterministic package timestamps.
The fake-config patch deliberately fails if it is built outside this flow
without `SOURCE_DATE_EPOCH`; it never reads the runner's wall clock.

Use `--dry-run` or `--smoke` on supported stages to inspect the resolved plan
without doing a full compile. A modules-only local build must point at a
matching kernel output containing at least `.config`, `Module.symvers`,
`System.map`, and the kernel release metadata.

In-tree module outputs are part of the official Kleaf build contract. During
configuration, each allowlisted `=m` symbol is resolved to an exact `.ko` path.
Paths not already in the locked GKI list are recorded as
`OP13_MODULE_IMPLICIT_OUTS` in `kernel_platform/common/modules.bzl` and appended
to the audited arm64 targets' `module_implicit_outs` in `BUILD.bazel`. The OOS
16 source wrapper builds both `kernel_aarch64` and `kernel_aarch64_16k`, so the
integrator extends both; OOS 15 extends only `kernel_aarch64`.
Because OnePlus's stock MSM dist omits module-staging outputs, the
full-preimage-pinned `msm_kernel_la.bzl` integration exposes the final mixed
target's `modules_staging_archive` output group and adds that single archive
to the official `sun/perf` dist data. The archive already applies Kleaf's
device-over-base module precedence. The builder extracts only the audited
declared paths and writes a deterministic `modules.order`; it never performs a
separate in-tree rebuild outside Kleaf.

Locked external modules use the other official OnePlus path. The module stage
runs `kernel_platform/build/brunch` to generate the pinned `build.config`, then
invokes `kernel_platform/build/build_module.sh` with the preserved kernel kit,
`Module.symvers`, target, and variant. Emitted `.ko` files are staged only after
their vermagic matches the in-tree module release; `depmod -e` checks the
combined staging tree for unresolved symbols.

## Configuration pipeline

The configure stage applies the declared fragments with the kernel's
`scripts/config`, runs `olddefconfig`, applies the selected O2/O3 and Thin/Full
LTO overrides, and checks final symbols. The build stops if an enabled feature
loses a required symbol through unmet Kconfig dependencies.

`root=none` is an explicit run override: root and SUSFS patches/configuration
are omitted even though a profile records both supported root implementations.

## Build targets

- `kernel`: run the pinned official `sun perf` entry point and publish `Image`
  plus reusable kernel lineage. Module-scoped feature fragments and the module
  ZIP are omitted from this output target.
- `modules`: do not compile a new kernel. Stage the selected in-tree modules
  already emitted through the prerequisite artifact's official Kleaf
  `module_implicit_outs`, then build locked external modules through OnePlus's
  official `build/build_module.sh` helper. The exact, versioned `mixed` kernel
  artifact must match manifest, feature, root, optimization, LTO, `.config`,
  and `Module.symvers` lineage.
- `mixed`: run the pinned official `sun perf` entry point, then build and
  validate the Kleaf-emitted in-tree modules and officially built external
  modules against that exact kernel output. This is the complete supported
  kernel-plus-modules target.
- `monolithic`: reserved and stopped during configuration. The pinned OnePlus
  13 official entry point is a GKI mixed-build pipeline and has no monolithic
  source target. This selector is never treated as an alias for `mixed`; it can
  be enabled only after a pinned OnePlus 13 monolithic entry point and its
  artifact/boot checks are added.

All enabled modes retain the same source profile. Do not combine DTB, DTBO, DLKM,
`Module.symvers`, or modules from another manifest or OTA.

## Diagnostics

Debug mode uploads the resolved manifest, build contexts, final `.config`,
patch log/rejects, compiler logs, `vmlinux`, `System.map`, `Module.symvers`,
modules, disk reports, warning scan, cache statement, and SHA-256 files. Failed
builds upload available diagnostics even when `debug=false`.

See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) before rerunning a failed job.
