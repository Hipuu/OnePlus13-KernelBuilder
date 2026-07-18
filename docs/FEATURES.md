# Feature profiles

Feature selection is explicit. Each file in `configs/features/` contains the
same complete 52-flag catalog, a boolean value for every flag, ordered patch
manifests, Kconfig fragments, final required symbols, and locked external-module
IDs. Profiles do not inherit from one another.

## Profile comparison

| Capability | `wild` | `nethunter` | `full` |
| --- | :---: | :---: | :---: |
| KernelSU and KernelSU-Next choices | yes | yes | yes |
| SUSFS | yes | yes | yes |
| HMBIRD/Fengchi SCX | yes | no | yes |
| Baseband Guard | yes | yes | yes |
| Module overlay/interception | yes | no | yes |
| Droidspaces | yes | yes | yes |
| NTSync | yes | no | yes |
| Rust Binder | no | no | no |
| TMPFS XATTR/ACL | yes | yes | yes |
| Unicode and fake-config fixes | yes | no | yes |
| Oryon-specific optimization set | yes | no | yes |
| Memory, I/O, and scheduler optimization set | yes | yes | yes |
| O2/O3 and Thin/Full LTO controls | yes | yes | yes |
| BBR, BBRv3, and FQ | yes | yes | yes |
| CAKE and PIE | yes | no | yes |
| TTL/HL, IP sets, and IPv6 NAT | yes | yes | yes |
| Bluetooth, SDR, CAN, and USB serial | no | yes | yes |
| ATH, MT76, RTW88, and MemKernel modules | no | yes | yes |
| Wireless firmware bundle | no | yes | yes |
| Raw partition images | gated | gated | gated |

The `full` profile is a union, not an inheritance shortcut: its booleans and
requirements are independently recorded. `wild` reflects the OnePlus 13 Wild
feature set. `nethunter` reflects the EmberHeart hardware/module feature set
with its common root, Baseband Guard, Droidspaces, optimization, and networking
baseline.

## Root and SUSFS

`kernelsu` and `kernelsu-next` are the implemented root variants. Both are
resolved from `dependencies/lock.yml`, and build-time setup scripts are executed
from verified local checkouts. `root=none` omits root and SUSFS for that run.

SUSFS is supported for the OOS 15 profiles. It remains experimental on OOS 16
until the exact profile compiles and boots with both root variants and completes
the device-test protocol. Experimental OOS 16 output stays pre-release.

Rust Binder remains an explicit, false flag in all three profiles. The pinned
references do not provide its Kconfig/source implementation for SM8750 6.6.
Adding a fragment with an unknown Binder symbol must fail final configuration;
the feature may be enabled only with a pinned, reviewable implementation and a
matching runtime test.

## OnePlus and performance features

The Wild patch manifest covers HMBIRD/Fengchi SCX integration, module
overlay/interception, fake-config behavior, NTSync and Unicode compatibility,
Droidspaces, Baseband Guard, and the Oryon/memory/I/O/scheduler patch groups.
Every patch has a pinned upstream dependency and an explicit profile/KMI
condition.

Optimization and LTO are inputs rather than separate profiles. The configure
stage applies the profile defaults, then the selected `O2`/`O3` and
`thin`/`full` overrides, runs `olddefconfig`, and verifies the resulting state.

## Networking

The complete profile provides:

- BBR and the pinned BBRv3 backport;
- FQ, FQ-CoDel, CAKE, PIE, and FQ-PIE queueing;
- IPv4 TTL and IPv6 HL targets/matches;
- bitmap, hash, and list IP-set families;
- IPv6 NAT and masquerade support.

Kconfig requirements are checked in the final `.config`, after dependency
resolution, rather than assumed from fragment text.

## NetHunter hardware

Common-kernel configuration includes USB Bluetooth HCI, AirSpy and HackRF SDR,
VCAN and common CAN platform/SPI/USB adapters, and CH341, FTDI, and PL2303 USB
serial support.

The NetHunter fragment explicitly pins media subdriver autoselection off. The
locked OnePlus media Kconfig then resolves 37 ancillary tuner symbols to
modules when SDR support is enabled.
Those are retained as part of the resolved feature closure: the builder maps
their exact 38 Kbuild outputs (the simple-tuner symbol emits two files) into
Kleaf, packages them, and verifies them like the other in-tree modules. This
avoids both an undeclared-output build failure and silently removing useful
SDR-adjacent drivers.

The separate modules fragment covers ATH9K/10K/11K and MT76 families plus
SLCAN. RTW88 is built from its locked external checkout. MemKernel's reviewed
source files are copied from its locked checkout into the common tree; its
mutable setup script and binary/key material are excluded. The pinned
MemKernel source is MIT-licensed. All in-tree `=m` selections are resolved to
allowlisted `.ko` paths; outputs not already declared by GKI are added to the
locked common Kleaf arm64 targets through `module_implicit_outs`. OxygenOS 16
extends both `kernel_aarch64` and its `kernel_aarch64_16k` companion because the
official wrapper builds both dist targets; OxygenOS 15 extends only the 4 KiB
target.
The full-preimage-pinned MSM dist exports the final mixed target's
`modules_staging_archive`, preserving Kleaf's device-over-base precedence. The
builder extracts the audited declared paths and creates a deterministic
`modules.order`, without a separate in-tree module rebuild. RTW88 uses the
preserved kernel kit
and OnePlus's official `kernel_platform/build/build_module.sh` helper. Both
paths use the same final `.config`, kernel release, and `Module.symvers` as the
kernel artifact, so the ZIP contains the enabled Bluetooth, CAN, ATH, MT76,
MemKernel, and RTW88 modules. Verification checks recorded files, vermagic,
unresolved symbols, and `depmod` before packaging.

The wireless firmware ZIP is a separately checksummed release asset. Firmware
is never treated as kernel source and retains its upstream distribution terms.

## Build and artifact capabilities

All profiles support kernel-only, modules-only, and official mixed targets;
clean and debug execution; and custom branding/timestamps. The monolithic flag
is disabled in shipped profiles: the locked `sun perf` source contract is a
mixed GKI pipeline and publishes no monolithic OnePlus 13 entry point. Initial
packages are limited to `Image`, AnyKernel3, module and firmware bundles,
resolved provenance, checksums, and diagnostics.

Raw partition images stay disabled until exact region/OTA metadata records
partition sizes, command line, bootconfig, DTB/DTBO, module lists, and AVB
metadata and passes round-trip verification. Metadata from another device,
region, or OTA is not compatible evidence.
