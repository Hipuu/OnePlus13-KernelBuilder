# OnePlus 13 Kernel Builder

Reproducible GitHub Actions and local tooling for building OnePlus 13 (`dodge`)
kernels from the official OnePlus SM8750 source manifests. The repository is a
small build orchestrator: it downloads locked sources at build time instead of
committing a full kernel tree.

## Platform

| Property | Value |
| --- | --- |
| Device | OnePlus 13 (`dodge`) |
| SoC / target | Snapdragon 8 Elite, SM8750 / `sun` |
| Architecture | arm64 |
| KMI family | `android15-6.6` |
| Official build | `./kernel_platform/oplus/build/oplus_build_kernel.sh sun perf` |

The supported source profiles are:

| Profile | Official manifest | SUSFS status |
| --- | --- | --- |
| `oos15-cn` | `oneplus_13.xml` | supported |
| `oos15-global` | `oneplus_13_global.xml` | supported |
| `oos16` | `oneplus_13_b.xml` | experimental |

OxygenOS 16 for this device still uses the `android15-6.6` KMI family. Every
kernel, DTB/DTBO, module, and DLKM input in a build must come from the same
resolved manifest.

## Features

- Selectable, pinned KernelSU or KernelSU-Next integration with SUSFS hooks.
- HMBIRD/Fengchi SCX, Baseband Guard, module overlay/interception,
  Droidspaces, NTSync, TMPFS XATTR/ACL, Unicode fixes, and fake-config support.
- Oryon, memory, I/O, and scheduler optimizations; O2/O3 and Thin/Full LTO.
- BBR and BBRv3, FQ/CAKE/PIE queueing, TTL/HL, IP sets, and IPv6 NAT.
- NetHunter Bluetooth HCI, AirSpy/HackRF, CAN/VCAN/SLCAN and USB CAN,
  CH341/FTDI/PL2303 serial, ATH9K/10K/11K, MT76, RTW88, and MemKernel support.
- Kernel-only, modules-only, and official mixed build targets with clean,
  cached, debug, branding, and timestamp controls. A monolithic selector is
  retained as a fail-fast future gate because the locked OnePlus 13 sources do
  not publish a `sun` monolithic entry point; GitHub build/release inputs expose
  only the three supported targets.

See [the feature matrix](docs/FEATURES.md) and the machine-readable profiles in
[`configs/features`](configs/features).

Rust Binder is represented in the flag catalog but disabled in every shipped
profile: neither pinned reference provides an implementation for the SM8750
6.6 tree. It will remain off until a reviewable, pinned implementation exists.

## Build in GitHub Actions

No personal access token belongs in this repository or its workflow inputs.
Actions uses the run-scoped `${{ github.token }}` with least-privilege
permissions. For command-line dispatch, authenticate `gh` through its normal
credential store:

```bash
gh auth login
gh workflow run build.yml --repo Hipuu/OnePlus13-KernelBuilder \
  -f base=oos16 \
  -f root=kernelsu-next \
  -f profile=full \
  -f target=mixed \
  -f optimization=O2 \
  -f lto=thin \
  -f clean=true \
  -f cache=true \
  -f debug=true \
  -f pre_release=true
```

Watch and diagnose the run with:

```bash
gh run watch RUN_ID --repo Hipuu/OnePlus13-KernelBuilder --exit-status
gh run view RUN_ID --repo Hipuu/OnePlus13-KernelBuilder --log-failed
gh run rerun RUN_ID --repo Hipuu/OnePlus13-KernelBuilder --failed
```

Modules-only dispatches resolve the newest unexpired matching reusable kernel
artifact, or use the numeric run ID in the repository variable
`KERNEL_ARTIFACT_RUN_ID`. They never assume that a cache contains a compatible
`Module.symvers`, and strict lineage validation still applies.

For cached compilation, dispatch with `clean=false` and `cache=true`. The cache
contains verified dependency data only; source/output trees and kernel lineage
artifacts are excluded.

## Outputs

The initial release surface is deliberately limited to:

- kernel `Image`;
- AnyKernel3 ZIP;
- module ZIP containing the selected in-tree and external modules;
- wireless firmware bundle for NetHunter profiles;
- resolved manifest, build context, and SHA-256 checksums;
- debug logs and symbols when requested or when a build fails.

Raw `boot`, `vendor_boot`, `system_dlkm`, `vendor_dlkm`, DTB, and DTBO partition
images remain gated. They are not produced until exact OTA/region metadata is
recorded and passes both unpack/repack and round-trip verification.

## Documentation

- [Building locally and in Actions](docs/BUILDING.md)
- [Feature profiles](docs/FEATURES.md)
- [Installing a build](docs/INSTALLING.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)
- [Device test protocol](docs/DEVICE-TESTING.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Credits and upstream licenses](CREDITS.md)

Builds should be treated as experimental until their exact base, root variant,
and feature profile have a completed device-test record. Always keep the stock
firmware needed for rollback.

## License

The original orchestration in this repository is licensed under the
[MIT License](LICENSE). Downloaded kernels, patches, modules, packaging tools,
and other dependencies retain their upstream licenses and are not relicensed
by this repository. See [CREDITS.md](CREDITS.md).
