# OnePlus13-KernelBuilder — Agent Guide

> This file is written for AI coding agents that need to understand or modify this repository. The project uses English for all comments, documentation, and commit messages.

## 1. Project overview

`OnePlus13-KernelBuilder` is a GitHub Actions-based CI/CD pipeline that builds custom kernels for the **OnePlus 13** (Qualcomm SM8750, codename "sun"). It produces three main artifacts for each build:

- A flashable **AnyKernel3 ZIP** (`AK3_*.zip`)
- A raw ARM64 **`Image`**
- A loadable **wireless/CAN modules pack** (`kernel_modules_*.zip`) plus a firmware passthrough ZIP

The pipeline is a single-device fork of [WildKernels/OnePlus_KernelSU_SUSFS](https://github.com/WildKernels/OnePlus_KernelSU_SUSFS), pinned to a specific commit and stripped down to only OnePlus 13 variants. Key feature toggles include:

- KernelSU / KernelSU-Next root support
- SUSFS (optional)
- NetHunter inline configs and external Wi-Fi/CAN drivers
- HMBIRD scheduler patches, BBR/BBRv3, TTL target, IP_SET, NTSync, and other kernel tweaks

### Supported variants

All six variants share the same SoC (SM8750), Android generation (`android15`), and manifest branch (`wild/sm8750`), but differ in kernel version and OxygenOS generation:

| Config | Kernel | OS | Manifest |
|---|---|---|---|
| `configs/OP13-6.6.89.json` | 6.6.89 | A16 | `manifests/a16/oneplus_13_6.6.89_w.xml` |
| `configs/OP13-6.6.118.json` | 6.6.118 | A16 | `manifests/a16/oneplus_13_6.6.118_w.xml` |
| `configs/OP13-6.6.66.json` | 6.6.66 | A15 | `manifests/a15/oneplus_13_6.6.66_v.xml` |
| `configs/OP13-6.6.30.json` | 6.6.30 | A15 | `manifests/a15/oneplus_13_6.6.30_v.xml` |
| `configs/OP13-CPH-6.6.89.json` | 6.6.89 | A15 global | `manifests/a15/oneplus_13_global_6.6.89_v.xml` |
| `configs/OP13-CPH-6.6.56.json` | 6.6.56 | A15 global | `manifests/a15/oneplus_13_global_6.6.56_v.xml` |

### Technology stack

- **CI/CD**: GitHub Actions (workflow + composite actions)
- **Languages**: Bash (runner scripts), POSIX `sh` (on-device loader), Python 3 (manifest/archive downloader), YAML
- **Build system**: Android GKI/Kbuild, LLVM/Clang toolchains, ccache, `repo`-style manifests
- **Toolchain**: ZyC Clang 19 by default, or the manifest-pinned Clang
- **Target architecture**: ARM64 (`aarch64`)
- **Packaging**: AnyKernel3 (flashable ZIP), plain ZIP for modules

### Repository layout

```
.github/
  workflows/build-oneplus13-kernel.yml   # Top-level workflow (workflow_dispatch)
  actions/
    build-kernel/
      action.yml                         # Composite action doing the actual build
      files/nethunter-wifi.sh            # On-device module loader (POSIX sh)
    kernel-source-sync/action.yml        # Downloads pinned sources/archives from manifest
    cache/restore/action.yml             # Release-backed ccache restore
    cache/save/action.yml                # Release-backed ccache save
configs/                               # JSON device configs (one per variant)
manifests/                             # XML repo manifests (one per variant)
validate_workflow.sh                   # Local static validation script
```

## 2. Build and test commands

### Local validation (run this before pushing workflow changes)

```bash
./validate_workflow.sh
```

Checks performed:
- YAML/JSON/XML parseability
- `actionlint` linting (if installed; a stale 10-input cap rule is suppressed)
- Workflow has a `workflow_dispatch` trigger and every composite action has `name`, `description`, and `runs`
- All local `uses: ./...` references resolve to a real `action.yml`
- Every `configs/OP13-*.json` targets `wild/sm8750` and names an existing manifest
- Manifests pin all critical repos to full 40-character SHAs
- No boot/vendor/DLKM image construction commands exist
- Pinned toolchain-cache assets are reachable (skipped with `SKIP_NETWORK=1`)
- Required files are tracked by git
- The on-device loader is POSIX `sh` with LF line endings and uses `#!/system/bin/sh`

### Running a build

Builds are only triggered through GitHub Actions; there is no local build script.

From the CLI with the `gh` tool:

```bash
# Default build (6.6.89 A16, KSUN + SUSFS + NetHunter + wireless modules)
gh workflow run "Build OnePlus 13 Kernel" -R Hipuu/OnePlus13-KernelBuilder

# Single non-default variant
gh workflow run "Build OnePlus 13 Kernel" -R Hipuu/OnePlus13-KernelBuilder -f kernel_version="6.6.118 A16"

# All six variants in parallel
gh workflow run "Build OnePlus 13 Kernel" -R Hipuu/OnePlus13-KernelBuilder -f kernel_version=all
```

### Workflow debug helper

```bash
./debug_workflow.sh [status|logs|failed|watch|rerun|artifacts|download] [run_id]
```

This wraps the `gh` CLI to inspect recent runs, fetch logs, or download artifacts.

## 3. Code style guidelines

### YAML / GitHub Actions

- Keep the top-level workflow dispatch inputs aligned with the `plan` job's `case` statement in `.github/workflows/build-oneplus13-kernel.yml`. If you add a variant, add both a config and a manifest option and update both places.
- Composite actions must declare `name`, `description`, and `runs`.
- Use `set -euo pipefail` in every bash step.
- Use `::group::`/`::endgroup::` for long log sections.

### Shell scripts

- **Runner scripts** (`.github/actions/**/*.yml` inline steps): Bash is fine.
- **On-device script** `.github/actions/build-kernel/files/nethunter-wifi.sh`: Must be **POSIX `sh`**. Do not use `[[ ]]`, `local`, arrays, `mapfile`, `<<<`, or `awk`. Android's toybox has no `awk` and the shell is often `mksh`/toybox.
- All `.sh` files must keep **LF** line endings. `.gitattributes` enforces this; do not override it.

### JSON / XML

- Config files are sorted by key and should stay that way for readability.
- Manifests must pin critical repositories to full 40-character SHAs. The validator checks this.

### Comments and documentation

- Explain *why* a change is made, not just what it does.
- Keep `README.md`, `TESTING.md`, and this file in sync when behavior changes.

## 4. Testing instructions

1. **Always run the validator after editing workflows, actions, configs, or manifests:**

   ```bash
   ./validate_workflow.sh
   ```

2. **For offline work**, skip the network asset-reachability check:

   ```bash
   SKIP_NETWORK=1 ./validate_workflow.sh
   ```

3. **To lint the workflow with `actionlint`** (optional but recommended):

   ```bash
   actionlint -ignore 'maximum number of inputs for "workflow_dispatch" event'
   ```

4. **Trigger a live build** only after local validation passes.

5. **Test module-pack changes** by:
   - Verifying `modules.dep` has one line per packaged module.
   - Running the loader logic against a real `modules.dep` with stubbed `insmod`/`resident`.
   - Keeping the script POSIX-clean (validated by `validate_workflow.sh`).

## 5. Security considerations

- **Do not commit personal access tokens.** The workflow uses the automatically provided `secrets.GITHUB_TOKEN`. If you ever pasted a PAT anywhere, revoke it immediately.
- **No boot/vendor/DLKM images** are produced or constructed. The validator explicitly blocks `mkbootimg`, `avbtool`, `boot.img`, `vendor_boot`, `vendor_dlkm`, and `system_dlkm` references. Do not add them.
- **Tooling is downloaded at build time** from public GitHub releases and the configured manifest repos. Pins are verified by the validator; do not loosen SHA pinning.
- **The on-device loader runs as root.** It displaces the platform Wi-Fi stack when loading `mac80211`-based drivers. Keep the loader simple and auditable; avoid shell injection through driver names or module paths.
- **Artifact retention is 90 days.** Releases use `softprops/action-gh-release@v2` with an automatically generated tag. Be careful when changing release logic to avoid accidental public publishing.
- **Cache artifacts are stored in GitHub Releases.** The custom cache actions upload and download archives tied to a release tag. Do not expose these archives or the release token beyond the workflow's intended scope.

## 6. Common agent tasks

### Adding a new device variant

1. Create a new JSON config under `configs/`.
2. Add the corresponding XML manifest under `manifests/<os>/`.
3. Update the `plan` job's `case` statement in `.github/workflows/build-oneplus13-kernel.yml`.
4. Add the new config/manifest to `validate_workflow.sh`.
5. Run `./validate_workflow.sh`.

### Changing patch or KernelSU logic

- Kernel patching happens inside `.github/actions/build-kernel/action.yml`. Keep patches ordered and idempotent where possible.
- KernelSU ref resolution is done in the main workflow before passing a SHA to the composite action. Do not let `setup.sh` auto-pick the latest tag, because that can mismatch SUSFS expectations.

### Modifying the wireless module pack

- The module configs are appended to `gki_defconfig` inside `.github/actions/build-kernel/action.yml`.
- The packaging logic computes a dependency closure from `modules.dep` and ships a flat ZIP plus `nethunter-wifi.sh`.
- Any change to the loader must pass `dash -n` and the bashism/awk/CRLF checks in `validate_workflow.sh`.
