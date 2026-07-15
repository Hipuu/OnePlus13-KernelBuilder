# NetHunter integration notes

The profile keeps integration declarative and commit-locked:

- Bluetooth HCI, AirSpy/HackRF, CAN/VCAN/SLCAN and USB CAN, CH341/FTDI/PL2303,
  ATH9K/ATH10K/ATH11K, and MT76 are in the locked OnePlus Android 15 6.6 common
  source. Their Kconfig fragments therefore enable existing in-tree drivers.
- `rtw88` is built out of tree by `build_external_modules` from the exact Git
  commit in `dependencies/lock.yml`. The module stage uses OnePlus's pinned
  `kernel_platform/build/brunch` entry point to generate `build.config`, then
  invokes the official `kernel_platform/build/build_module.sh` helper with the
  preserved kernel kit and `Module.symvers`. It stages only the emitted `.ko`
  files; verification checks their vermagic and unresolved symbols with
  `depmod -e`.
- MemKernel is integrated in tree by `patches/series/nethunter.yml`. The series
  copies only its C sources, headers, Kconfig, and Makefile from the verified
  checkout. It excludes the upstream setup script, which performs network
  access and randomizes names, and enables the stable `CONFIG_MEMKERNEL=m`
  module name instead. Configuration maps that symbol to
  `drivers/memkernel/memkernel.ko`, adds it to the locked common Kleaf arm64
  targets' `module_implicit_outs`, and records the resolved output set in build
  lineage. OOS 16 updates both its 4 KiB and 16 KiB-page companion targets.
  The full-preimage-pinned MSM dist integration exports the final mixed
  target's `modules_staging_archive` through the official `sun/perf` build.
  The module stage extracts that audited output to
  `drivers/memkernel/memkernel.ko` and applies the same vermagic and
  unresolved-symbol checks used for external modules. No separate in-tree
  module rebuild occurs. The pinned MemKernel source is MIT-licensed.

Shared BBG and Android 15/6.6 BBRv3 integration belongs to
`patches/series/common.yml`; it must run once before this series. The final
`.config` assertion is authoritative: a requested symbol that is missing or
disabled fails the build instead of being silently advertised.

No prebuilt kernel module, signing key, helper executable, or unverified
download is copied by this series.
