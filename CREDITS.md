# Credits and licenses

This project combines ideas and feature coverage from the following reference
projects while implementing a new, reproducible orchestration layer:

- [Hipuu/OnePlus_KernelSU_SUSFS](https://github.com/Hipuu/OnePlus_KernelSU_SUSFS),
  reference snapshot `7ea1d5058255fba3cf8e836d0c6c27c9546b7f6c`.
- [nullptr-t-oss/EmberHeart_OnePlus11](https://github.com/nullptr-t-oss/EmberHeart_OnePlus11),
  reference snapshot `2a04867ce49ab123ab406fc0785555ba80ed4ea9`.

The build also depends on or integrates work from:

- [OnePlusOSS kernel sources and manifests](https://github.com/OnePlusOSS/kernel_manifest)
- [the Linux kernel](https://www.kernel.org/)
- [Android Common Kernels](https://android.googlesource.com/kernel/common/)
- [KernelSU](https://github.com/tiann/KernelSU)
- [KernelSU-Next](https://github.com/KernelSU-Next/KernelSU-Next)
- [SUSFS](https://gitlab.com/simonpunk/susfs4ksu)
- [WildKernels kernel patches](https://github.com/WildKernels/kernel_patches),
  pinned at `2ee34500cb4c3ee954ba36090e11f6ff08b3ec2f`. That snapshot has no
  repository-wide license file; individual file-level license identifiers and
  patch notices are preserved, and no repository-wide license is inferred.
- [Baseband Guard](https://github.com/vc-teahouse/Baseband-guard)
- [AnyKernel3](https://github.com/osm0sis/AnyKernel3)
- [rtw88](https://github.com/lwfinger/rtw88)
- [MemKernel](https://github.com/Poko-Apps/MemKernel), pinned at
  `556891806e7907c135574f398d2a20ca0e7ff27e` under its
  [MIT License](https://github.com/Poko-Apps/MemKernel/blob/556891806e7907c135574f398d2a20ca0e7ff27e/LICENSE)
- [NetHunter wireless firmware packaging](https://github.com/nullptr-t-oss/Nethunter-Wireless-Firmware)
- [easimon/maximize-build-space](https://github.com/easimon/maximize-build-space),
  pinned at `c28619d8999a147d5e09c1199f84ff6af6ad5794` under its
  [MIT License](https://github.com/easimon/maximize-build-space/blob/c28619d8999a147d5e09c1199f84ff6af6ad5794/LICENSE).

The root [MIT License](LICENSE) covers only original orchestration,
configuration metadata, schemas, and documentation contributed to this
repository. It does not relicense anything fetched during a build. The Linux
kernel retains its GPL-2.0 terms; downloaded or imported patches, modules,
firmware, and tools retain the file-level and upstream terms actually published
for them. In particular, the pinned WildKernels snapshot publishes no
repository-wide license, so this project does not assign or guess one. Consult
the locked dependency source and its license notices before redistributing an
artifact.

When a patch or file is imported rather than fetched, keep its original header,
license, authorship, and commit attribution beside it. If an upstream license
is unclear, do not copy the material into this repository; record the upstream
reference and resolve the license first.
