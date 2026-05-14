# OnePlus 13 Kernel Builder

Custom kernel builder for **OnePlus 13** (SM8750/Snapdragon 8 Elite) combining the best from:
- [WildKernels/OnePlus_KernelSU_SUSFS](https://github.com/WildKernels/OnePlus_KernelSU_SUSFS) - Patches, optimizations, HMBIRD SCX
- [nullptr-t-oss/EmberHeart_OnePlus11](https://github.com/nullptr-t-oss/EmberHeart_OnePlus11) - Build system, modules, Nethunter

## Features

- **KernelSU Next** - Root solution with manager support
- **SUSFS v2.1.0** - Stealth/hiding capabilities
- **Baseband Guard (BBG)** - LSM security module
- **BBR v1** - TCP congestion control
- **Wireguard** - VPN support
- **HMBIRD SCX** - Scheduler extensions for SM8750
- **NTSync** - NT synchronization primitives
- **Nethunter Support** - Full wireless driver modules + firmware
- **25+ Performance Patches** - Memory, filesystem, CPU, network optimizations
- **IP Set / TTL Target** - Network filtering support
- **MemKernel** - Physical memory r/w driver

## Output Artifacts

| Artifact | Description |
|----------|-------------|
| `AnyKernel3_OP13_*.zip` | Flashable kernel ZIP (AnyKernel3) |
| `kernel_modules_OP13.zip` | All kernel modules (non-default .ko files) |
| `Nethunter-Wireless-Firmware-*.zip` | Magisk module with wireless firmware |

## Supported Variants

| Model | Variant | SoC | Kernel |
|-------|---------|-----|--------|
| OP13 | China (PJZ) | SM8750 (sun) | android15-6.6 |
| OP13-CPH | Global | SM8750 (sun) | android15-6.6 |

## Usage

1. Go to **Actions** tab
2. Select **Build and Release OnePlus 13 Kernel**
3. Choose your options (manifest, model, optimization level)
4. Click **Run workflow**
5. Download artifacts or create a release

## Credits

- [WildKernels](https://github.com/WildKernels) - Kernel patches and optimization
- [nullptr-t-oss](https://github.com/nullptr-t-oss) - EmberHeart build system
- [KernelSU-Next](https://github.com/KernelSU-Next) - Root solution
- [simonpunk/susfs4ksu](https://gitlab.com/simonpunk/susfs4ksu) - SUSFS
- [TheWildJames](https://github.com/TheWildJames) - AnyKernel3 and kernel patches
- [Poko-Apps](https://github.com/Poko-Apps) - MemKernel
- [lwfinger](https://github.com/lwfinger) - RTW88 drivers
