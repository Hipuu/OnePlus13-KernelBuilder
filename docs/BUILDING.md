# Building

The build is driven by three independent selectors:

- base: `oos15-cn`, `oos15-global`, or `oos16`;
- feature profile: `wild`, `nethunter`, or `full`;
- root: `kernelsu`, `kernelsu-next`, or `none`.

Build target, optimization, and LTO are separate choices. The default GitHub
Actions build is OOS 16, full features, KernelSU-Next, mixed output, O2, and
Thin LTO.

## Reproducible inputs

`dependencies/lock.yml` pins every dependency. Each release profile points to
a repository-owned file under `manifests/lockfiles/`; every project revision in
those XML files is a full commit SHA. The original manifest URL, branch, file,
and pinned manifest-repository revision remain in the profile for provenance
and source monitoring.

`scripts/sync-sources.sh` must initialize from the locked manifest. After sync,
the build records `repo manifest -r`, the lock file, and a build context. A
source-monitor run may report an upstream change, but it never edits the lock.

## GitHub Actions

From the Actions page, run **Build OnePlus 13 kernel** and select:

| Input | Values | Default |
| --- | --- | --- |
| `base` | `oos16`, `oos15-global`, `oos15-cn` | `oos16` |
| `root` | `kernelsu-next`, `kernelsu`, `none` | `kernelsu-next` |
| `profile` | `full`, `wild`, `nethunter` | `full` |
| `target` | `kernel`, `modules`, `mixed` | `mixed` |
| `optimization` | `O2`, `O3` | `O2` |
| `lto` | `thin`, `full` | `thin` |
| `clean` | boolean | `true` |
| `cache` | boolean | `true` |
| `debug` | boolean | `false` |
| `pre_release` | boolean | `true` |

Cached Actions mode is `clean=false` and `cache=true`. The cache key includes
the runner OS; dependency-lock, base-profile, and feature-profile hashes; and
the selected base, root, and feature profile. Only `.cache/op13` is cached;
source/output trees, `Module.symvers`, and kernel lineage metadata are excluded.

Set optional repository variables `KERNEL_BRANDING` and `BUILD_TIMESTAMP` for
single-line build metadata. A manual modules-only build resolves the newest
unexpired matching kernel artifact. Set `KERNEL_ARTIFACT_RUN_ID` to a numeric
run ID to choose a specific matching artifact instead.

Dispatch from an authenticated GitHub CLI session:

```bash
gh workflow run build.yml --repo Hipuu/OnePlus13-KernelBuilder \
  -f base=oos15-global \
  -f root=kernelsu \
  -f profile=wild \
  -f target=kernel \
  -f optimization=O2 \
  -f lto=thin \
  -f clean=true \
  -f cache=true \
  -f debug=true \
  -f pre_release=true
```

The `release.yml` workflow requires `tag`, then base, root, profile, target,
optimization, LTO, clean, debug, and pre-release inputs. GitHub's dispatch cap
leaves out a separate cache input: the called build defaults cache on, while
the release default `clean=true` skips restoration. Publishing occurs only
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
