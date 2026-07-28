# OnePlus 13 Kernel Builder

Automated GitHub Actions workflow to build custom kernels for OnePlus 13 with KernelSU and SUSFS support.

## Features

This kernel builder combines features from both [WildKernels/OnePlus_KernelSU_SUSFS](https://github.com/WildKernels/OnePlus_KernelSU_SUSFS) and [nullptr-t-oss/EmberHeart_OnePlus11](https://github.com/nullptr-t-oss/EmberHeart_OnePlus11), specifically optimized for **OnePlus 13 only**.

### Supported Features

- ✅ **KernelSU variants**: KernelSU-Next (ksun) and original KernelSU (ksu)
- ✅ **SUSFS integration**: Full SUSFS support with automatic version detection (v1.5.8 - v2.1.0)
- ✅ **HMBIRD scheduler patches**: SM8750 (Snapdragon 8 Elite) Fengchi scheduler optimizations
- ✅ **Optimization levels**: O2/O3 compiler optimizations
- ✅ **Link Time Optimization**: Thin or Full LTO
- ✅ **Memory optimizations**: Prefetch, memcmp, cache pressure, aligned operations
- ✅ **VFS optimizations**: Wakeup time, file operations
- ✅ **Network features**: BBR/BBRv3 TCP congestion control, IP_SET, TTL manipulation
- ✅ **Advanced features**: NTSync, Unicode support, module overlay mechanism
- ✅ **Custom ccache**: 12GB cache with compression for faster rebuilds
- ✅ **Manual hooks**: Automatic fallback for older KernelSU versions
- ✅ **AnyKernel3 packaging**: Ready-to-flash ZIP files

### What's NOT Included

- ❌ **boot.img builder**: Excluded as requested - only AnyKernel3 ZIPs are generated
- ❌ **Other devices**: This workflow is hardcoded for OnePlus 13 only

## Device Information

- **Model**: OnePlus 13
- **SoC**: Qualcomm Snapdragon 8 Elite (SM8750 / sun)
- **Android Version**: Android 15
- **Kernel Version**: 6.6.89
- **OS Version**: OxygenOS 16 (A16)
- **Manifest**: `oneplus_13_6.6.89_w.xml`
- **Branch**: `oneplus/android15`

## Usage

### Quick Start

1. Fork this repository
2. Go to **Actions** tab → **Build OnePlus 13 Kernel**
3. Click **Run workflow**
4. Configure options:
   - Select KernelSU variant (ksun/ksu)
   - Enable/disable SUSFS
   - Choose optimization level
   - Set custom kernel name (optional)
5. Click **Run workflow** button
6. Wait for build to complete (~30-60 minutes)
7. Download AnyKernel3 ZIP from **Artifacts**

### Workflow Inputs

| Input | Description | Default |
|-------|-------------|---------|
| `ksu_variant` | KernelSU variant (ksun or ksu) | `ksun` |
| `ksu_branch` | KernelSU branch/tag/commit | (latest dev/main) |
| `use_susfs` | Enable SUSFS integration | `true` |
| `susfs_branch` | SUSFS branch/commit | (auto: gki-android15-6.6) |
| `optimize_level` | Compiler optimization (O2/O3) | `O3` |
| `lto` | Link Time Optimization (thin/full) | `thin` |
| `use_opt_patches` | Apply optimization patches | `true` |
| `kernel_uname` | Custom kernel name suffix | `OP13-WILD` |
| `build_timestamp` | Custom build timestamp | (current time) |
| `clang_version` | Clang version | `default` |
| `clean_build` | Clean build (no ccache) | `false` |
| `make_release` | Create GitHub release | `false` |
| `debug` | Upload debug artifacts | `false` |

### Installation

1. Download the AnyKernel3 ZIP from workflow artifacts or releases
2. Boot into custom recovery (TWRP, OrangeFox, etc.)
3. Flash the ZIP file
4. Reboot system
5. Install KernelSU Manager app from:
   - KernelSU-Next: https://github.com/KernelSU-Next/KernelSU-Next/releases
   - Original KSU: https://github.com/tiann/KernelSU/releases

## Build Details

### Source

Kernel source is fetched using Google's `repo` tool from OnePlusOSS:
- **Manifest repository**: https://github.com/OnePlusOSS/kernel_manifest.git
- **Branch**: `oneplus/android15`
- **Manifest file**: `oneplus_13_6.6.89_w.xml`

### Toolchain

- **Compiler**: Clang (auto-detected from kernel prebuilts)
- **Architecture**: ARM64
- **Cross-compile**: aarch64-linux-gnu
- **Ccache**: Custom ECS-enabled ccache with 12GB cache and level-3 compression

### Patches Applied

**KernelSU Integration:**
- Setup script from KernelSU-Next or original KernelSU repo
- Version calculation based on commit count
- Manual hooks for older versions (< v12884)

**SUSFS Integration** (if enabled):
- Version-specific KSU compatibility patches (v1.5.8 - v2.1.0)
- Main SUSFS kernel patch for android15-6.6
- Multi-manager and multi-sepolicy support (v2.1.0+)

**Optimizations** (if enabled):
- Memory: optimized operations, aligned structures, cache pressure reduction
- VFS: minimized wakeup time, file struct alignment
- Network: TCP NODELAY, BBR congestion control
- Math: optimized int_sqrt, memcmp

**Device-specific:**
- HMBIRD Fengchi scheduler patches for SM8750

### Build Environment

- **Runner**: Ubuntu latest (GitHub-hosted)
- **Build space**: ~60GB (maximized)
- **Parallel jobs**: $(nproc) - all available CPU cores
- **Build time**: ~30-60 minutes depending on configuration

## Outputs

### Artifacts

1. **AnyKernel3 ZIP**: Flashable kernel package
   - Format: `AK3_OP13_A16_android15-6.6_KSUN_XXXX_SUSFS_vX.X.X.zip`
   - Contains: kernel Image, AnyKernel3 scripts

2. **Kernel Image**: Raw kernel binary
   - Path: `Image_OP13_<version>`

3. **Debug artifacts** (if enabled):
   - `System.map`: Kernel symbol map
   - `vmlinux`: Uncompressed kernel with debug symbols

### Release Notes

When `make_release` is enabled, automatic releases are created with:
- Device and build configuration details
- Enabled features list
- SHA256 checksums
- Installation instructions

## Technical Details

### Directory Structure

```
.
├── .github/
│   ├── workflows/
│   │   └── build-oneplus13-kernel.yml    # Main workflow
│   └── actions/
│       ├── setup-environment/             # Dependencies & ccache
│       ├── kernel-source-sync/            # Repo tool sync
│       └── build-kernel/                  # Build logic
├── configs/
│   └── OP13-6.6.89.json                  # Device config
└── README.md
```

### Composite Actions

**setup-environment**
- Installs build dependencies (clang, repo, python, etc.)
- Downloads custom ccache with ECS support
- Configures ccache (12GB, compression level 3)
- Sets up Git and Python environment

**kernel-source-sync**
- Initializes repo tool
- Syncs kernel source from OnePlusOSS manifest
- Verifies source structure (kernel_platform, common, defconfig)
- Extracts kernel version

**build-kernel**
- Clones kernel patches repository
- Sets up KernelSU (ksun or ksu variant)
- Configures SUSFS if enabled
- Applies version-specific compatibility patches
- Applies optimization patches
- Applies HMBIRD scheduler patches
- Configures kernel defconfig
- Builds kernel with specified optimization level and LTO
- Packages AnyKernel3 ZIP
- Generates SHA256 checksums

### Key Differences from Source Repos

| Feature | WildKernels | EmberHeart | This Repo |
|---------|------------|------------|-----------|
| Device support | 60+ devices | 3 devices (OP11) | OnePlus 13 only |
| KSU variants | KSUN + KSU | KSUN only | KSUN + KSU |
| boot.img | ❌ | ✅ | ❌ (excluded) |
| Ccache size | 12GB | 5GB | 12GB |
| HMBIRD patches | ✅ | ❌ | ✅ (SM8750) |
| Config structure | JSON per device | JSON per device | Single OP13 JSON |
| Release automation | Full | Minimal | Full |

## Troubleshooting

### Build fails during source sync
- Check if OnePlusOSS manifest is available: https://github.com/OnePlusOSS/kernel_manifest/blob/oneplus/android15/oneplus_13_6.6.89_w.xml
- Ensure manifest branch and file names are correct

### Build fails during KernelSU setup
- Verify KernelSU branch exists (dev for ksun, main for ksu)
- Check if custom branch/commit is valid

### Build fails during SUSFS integration
- Verify SUSFS branch exists: https://gitlab.com/simonpunk/susfs4ksu/-/tree/gki-android15-6.6
- Check SUSFS version compatibility

### Kernel doesn't boot
- Try disabling optimization patches
- Switch from Full LTO to Thin LTO
- Try O2 instead of O3 optimization

### KernelSU manager doesn't detect kernel
- Verify you installed the correct manager (ksun vs ksu)
- Check kernel version matches KSU version

## Credits

- **WildKernels**: https://github.com/WildKernels/OnePlus_KernelSU_SUSFS
  - Multi-device kernel builder with extensive SUSFS integration
  - HMBIRD patches and optimization patches
  - Module overlay mechanism

- **nullptr-t**: https://github.com/nullptr-t-oss/EmberHeart_OnePlus11
  - EmberHeart kernel for OnePlus 11
  - Build system improvements and optimizations

- **KernelSU**: https://github.com/tiann/KernelSU
- **KernelSU-Next**: https://github.com/KernelSU-Next/KernelSU-Next
- **SUSFS**: https://gitlab.com/simonpunk/susfs4ksu
- **OnePlusOSS**: https://github.com/OnePlusOSS
- **AnyKernel3**: https://github.com/osm0sis/AnyKernel3

## License

This workflow configuration is provided as-is for educational and development purposes.

Kernel source code and patches retain their original licenses:
- Linux Kernel: GPL-2.0
- KernelSU: GPL-3.0
- SUSFS: GPL-2.0

## Security Notice

**⚠️ Important**: Never commit GitHub tokens or credentials to this repository. Use GitHub Secrets for sensitive data.

If you need to authenticate for releases:
1. Go to repository Settings → Secrets → Actions
2. Add `GITHUB_TOKEN` (automatically provided by GitHub Actions)
3. For custom tokens: Settings → Developer settings → Personal access tokens

---

**Built with ❤️ for OnePlus 13 users**
