# Testing & Debugging Guide

## Current Test Run

**Run ID**: 30423797772  
**URL**: https://github.com/Hipuu/OnePlus13-KernelBuilder/actions/runs/30423797772  
**Configuration**:
- KernelSU: ksun (latest dev)
- SUSFS: enabled (auto: gki-android15-6.6)
- Optimization: O3
- LTO: thin
- Opt patches: enabled
- Kernel name: OP13-WILD

**Status**: Running (monitored by background agent)

## Debug Tools

### Quick Commands

```bash
# Check current status
./debug_workflow.sh status

# Watch live progress
./debug_workflow.sh watch 30423797772

# Get failed step logs (if failure)
./debug_workflow.sh failed

# Download artifacts (if successful)
./debug_workflow.sh download
```

### Manual Debugging

```bash
# View workflow run
gh run view 30423797772 -R Hipuu/OnePlus13-KernelBuilder

# Get job details
gh run view --job=90485768527 -R Hipuu/OnePlus13-KernelBuilder

# Get full logs
gh run view --log -R Hipuu/OnePlus13-KernelBuilder 30423797772

# Get only failed logs
gh run view --log-failed -R Hipuu/OnePlus13-KernelBuilder 30423797772
```

## Common Failure Points & Fixes

### 1. Source Sync Failures

**Symptoms**: "repo sync" fails, manifest not found

**Possible causes**:
- Manifest file doesn't exist in OnePlusOSS
- Manifest branch incorrect
- Network timeout

**Debug**:
```bash
# Check if manifest exists
curl -I https://github.com/OnePlusOSS/kernel_manifest/raw/oneplus/android15/oneplus_13_6.6.89_w.xml

# If 404, try alternative manifest
git ls-remote https://github.com/OnePlusOSS/kernel_manifest.git oneplus/android15
```

**Fix**: Update manifest file name in workflow if OnePlusOSS changed naming

### 2. KernelSU Setup Failures

**Symptoms**: curl fails, setup.sh errors

**Possible causes**:
- KernelSU repo unreachable
- Branch doesn't exist
- Setup script syntax errors

**Debug**:
```bash
# Check if KernelSU-Next is accessible
curl -I https://raw.githubusercontent.com/KernelSU-Next/KernelSU-Next/dev/kernel/setup.sh

# Verify branch exists
git ls-remote https://github.com/KernelSU-Next/KernelSU-Next.git dev
```

**Fix**: Change KSU branch or use fallback repo

### 3. SUSFS Patch Failures

**Symptoms**: patch command fails, .rej files

**Possible causes**:
- SUSFS version mismatch
- Kernel version incompatibility
- Patch files not found

**Debug**:
Check which SUSFS version was detected and if patch directory exists for it

**Fix**: Add version-specific patch handling or update patches repository

### 4. Build Failures

**Symptoms**: make errors, compilation fails

**Possible causes**:
- Missing dependencies
- Clang version incompatibility
- Config errors
- LTO issues

**Debug**:
Look for specific compiler errors in logs

**Fixes**:
- Switch from Full LTO to Thin LTO
- Disable optimization patches
- Use O2 instead of O3
- Check defconfig modifications

### 5. Out of Space

**Symptoms**: "No space left on device"

**Possible causes**:
- GitHub runner disk full
- ccache too large

**Fix**: The workflow uses maximize-build-space action (should provide ~60GB)

## Test Matrix

### Minimal Test (Fast)
```bash
gh workflow run "Build OnePlus 13 Kernel" \
  -R Hipuu/OnePlus13-KernelBuilder \
  -f use_susfs=false \
  -f use_opt_patches=false \
  -f optimize_level=O2 \
  -f lto=thin
```

### Full Features Test (Current)
```bash
gh workflow run "Build OnePlus 13 Kernel" \
  -R Hipuu/OnePlus13-KernelBuilder \
  -f ksu_variant=ksun \
  -f use_susfs=true \
  -f optimize_level=O3 \
  -f lto=thin \
  -f use_opt_patches=true
```

### Alternative KSU Test
```bash
gh workflow run "Build OnePlus 13 Kernel" \
  -R Hipuu/OnePlus13-KernelBuilder \
  -f ksu_variant=ksu \
  -f ksu_branch=main \
  -f use_susfs=true
```

## Logs Analysis

### Key Log Sections to Check

1. **Setup environment** - Dependencies install correctly?
2. **Repo sync** - Source download complete?
3. **KernelSU setup** - Version detected correctly?
4. **SUSFS setup** - Branch checkout successful?
5. **SUSFS patches** - All patches applied cleanly?
6. **Toolchain setup** - Clang found and working?
7. **Build** - Compilation successful? Image generated?
8. **Package** - ZIP created correctly?

### Expected Build Duration

- Source sync: ~5-10 minutes
- KernelSU/SUSFS setup: ~2-5 minutes
- Kernel build: ~20-40 minutes (depends on optimization level)
- Packaging: ~1-2 minutes
- **Total: ~30-60 minutes**

## Artifacts

If successful, expect:
1. `AnyKernel3_OP13_*.zip` - Flashable kernel (main artifact)
2. `Image_OP13_*` - Raw kernel image
3. `Debug_OP13_*` - System.map, vmlinux (if debug=true)

## Next Steps After Success

1. Download AnyKernel3 ZIP
2. Verify SHA256 checksums in release notes
3. Test flash on OnePlus 13 device
4. Verify KernelSU manager detects kernel
5. Test SUSFS functionality

## Next Steps After Failure

1. Run `./debug_workflow.sh failed` to get error logs
2. Identify which step failed
3. Check "Common Failure Points" section above
4. Apply fix to workflow/action files
5. Commit and push changes
6. Rerun workflow

## Monitoring

Currently monitored by background agent. Will report:
- First failure encountered (immediate)
- Success with artifact links (after ~30-60 min)
- Timeout if exceeds 90 minutes
