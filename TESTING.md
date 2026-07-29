# Testing and Debugging

## Local static validation

Before publishing workflow changes, run the bundled validator:

```bash
./validate_workflow.sh
```

It checks that:

- Every workflow and composite `action.yml` parses as YAML, and the config/manifest parse as JSON/XML.
- `actionlint` reports no problems (skipped with a notice if not installed).
- The workflow has a `workflow_dispatch` trigger and every action declares `name`, `description`, and `runs`.
- All nested local composite-action references resolve to a real `action.yml`.
- `configs/OP13-6.6.89.json` targets `wild/sm8750` / `oneplus_13_6.6.89_w.xml`.
- `manifests/a16/oneplus_13_6.6.89_w.xml` pins the common kernel, Clang, kernel build-tools and WildKernels AnyKernel3 to full 40-character SHAs.
- No `mkbootimg`, `avbtool`, `boot.img`, `vendor_boot`, `vendor_dlkm`, or `system_dlkm` build command exists.
- The exact pinned toolchain-cache assets are reachable in the public WildKernels `toolchain-cache` release.
- Every file the workflow needs is tracked by git, so a fresh runner checkout has it.

Set `SKIP_NETWORK=1` to skip the asset-reachability check when offline.

## Live workflow commands

Check recent runs:

```bash
gh run list -R Hipuu/OnePlus13-KernelBuilder --workflow "Build OnePlus 13 Kernel" --limit 5
```

Watch a run:

```bash
gh run watch RUN_ID -R Hipuu/OnePlus13-KernelBuilder
```

Read failed-step logs:

```bash
gh run view RUN_ID -R Hipuu/OnePlus13-KernelBuilder --log-failed
```

Download successful artifacts:

```bash
gh run download RUN_ID -R Hipuu/OnePlus13-KernelBuilder -D artifacts
```

## Build profiles

Full default build:

```bash
gh workflow run "Build OnePlus 13 Kernel" -R Hipuu/OnePlus13-KernelBuilder
```

Minimal diagnostic build without SUSFS or optimization patches:

```bash
gh workflow run "Build OnePlus 13 Kernel" -R Hipuu/OnePlus13-KernelBuilder -f use_susfs=false -f use_opt_patches=false -f optimize_level=O2 -f lto=thin
```

Original KernelSU build:

```bash
gh workflow run "Build OnePlus 13 Kernel" -R Hipuu/OnePlus13-KernelBuilder -f ksu_variant=KSU -f ksu_branch=main
```

## Expected stages

1. Checkout the builder.
2. Install dependencies and configure ccache.
3. Load the single OnePlus 13 configuration.
4. Download pinned source/toolchain archives from the manifest.
5. Add KernelSU / KernelSU-Next.
6. Apply SUSFS (optional), HMBIRD, module-overlay, optimization, networking, NTSync and other configured patches.
7. Generate `gki_defconfig`, apply O2/O3 and LTO choices, and compile `Image`.
8. Package the WildKernels AnyKernel3 ZIP.
9. Upload AnyKernel3, raw `Image`, and optional debug artifacts.

## Expected artifacts

- `AK3_OP13_A16_...zip` — flashable AnyKernel3 package.
- `Image_OP13_A16_...` — raw ARM64 kernel `Image`.
- Debug artifact(s) when `debug=true`.

The workflow intentionally does not create boot, vendor boot, or DLKM images.

## Historical failures

The original hand-written action failed for several independent reasons: Ubuntu 24.04 package names, incorrect OnePlus manifest branch/name, incorrect `common/` path, invalid YAML from a heredoc, guessed SUSFS/HMBIRD paths, wrong KernelSU checkout directory and use of the generic osm0sis AnyKernel3 template. That action was removed and replaced with the pinned WildKernels pipeline; these old run IDs are not representative of the current implementation.
