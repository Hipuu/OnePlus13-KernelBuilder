# Device testing protocol

A compile is only the first gate. A profile is ready for a stable release after
the exact base/root/feature combination boots on matching firmware and the
relevant root, module, and hardware tests pass.

## Required test matrix

At minimum, retain separate records for:

| Base | Root variants | Required result |
| --- | --- | --- |
| OOS 15 China | KernelSU, KernelSU-Next | compile and boot |
| OOS 15 Global | KernelSU, KernelSU-Next | compile and boot |
| OOS 16 | KernelSU, KernelSU-Next | compile and boot; SUSFS remains experimental until both pass |

The full profile is the parity gate. Wild- and NetHunter-only records are still
needed when their patch/configuration paths differ from full. O2/O3, Thin/Full
LTO, and non-default build targets need targeted coverage before being called
stable.

## Preflight

- Record device model/codename, region, full OxygenOS build number, slot, and
  build fingerprint.
- Confirm a tested stock rollback path and retain the exact same-OTA stock
  package/images.
- Verify `SHA256SUMS` and record the package digest, workflow run ID, repository
  commit, resolved-manifest digest, base, root, feature profile, optimization,
  LTO, and target.
- Start with battery power sufficient for repeated boot/recovery cycles and no
  optional USB peripherals attached.

## Boot and baseline

After the first successful Android boot, capture:

```bash
adb shell uname -a
adb shell cat /proc/version
adb shell getprop ro.product.device
adb shell getprop ro.build.fingerprint
adb shell getprop ro.boot.slot_suffix
adb shell dmesg -T > dmesg-first-boot.txt
adb logcat -b all -d > logcat-first-boot.txt
```

Check cold boot, warm reboot, suspend/resume, charging, Wi-Fi, Bluetooth, camera,
audio, cellular registration/data, and basic storage I/O. Record regressions
even if Android eventually reaches the launcher.

## Root and feature checks

- Confirm that the manager matching the selected root variant reports the
  expected pinned version and that a controlled `su` request succeeds.
- When SUSFS is enabled, verify its reported version and each required hook
  without enabling unrelated third-party modules. Record OOS 16 results as
  experimental until the matrix is complete.
- Confirm NTSync is present for Wild/full profiles and run a minimal userspace
  consumer test.
- Confirm Baseband Guard is registered in the active LSM order.
- Exercise Droidspaces/user-namespace behavior and check `dmesg` for the known
  OnePlus task/IPC paths.
- Record scheduler/SCX availability for HMBIRD/Fengchi builds and compare idle,
  foreground, and sustained-load behavior with a stock baseline.

## Module integrity

Before hardware tests, compare every tested module with the running kernel:

```bash
adb shell uname -r
adb shell su -c 'modinfo -F vermagic MODULE_NAME'
adb shell su -c 'modprobe --dry-run MODULE_NAME'
adb shell su -c 'dmesg -T | tail -n 200'
```

The build-side record must also show successful unresolved-symbol checks and
`depmod`. A forced module load is a failure, not a pass.

## NetHunter hardware coverage

Use representative, identified adapters and record USB IDs, driver/module,
firmware version, test command, and result.

| Area | Minimum evidence |
| --- | --- |
| Bluetooth HCI | enumerate one USB HCI adapter and complete scan/connect cycle |
| SDR | enumerate and capture samples with AirSpy and HackRF hardware when available |
| CAN | create a VCAN interface; test SLCAN; enumerate and exchange frames through one USB CAN adapter |
| USB serial | open/transfer through representative CH341, FTDI, and PL2303 adapters |
| ATH | load and exercise representative ATH9K/ATH10K hardware; verify ATH11K module loading where supported |
| MT76 | load and exercise a representative supported USB/PCIe adapter |
| RTW88 | load the pinned external module and complete scan/association/traffic |
| MemKernel | load/unload the pinned module and check logs for symbol or lifecycle errors |

For wireless adapters, test managed operation and monitor-mode/injection
capability only where the adapter/firmware supports it. A module merely
appearing in the ZIP does not count as hardware support.

## Networking and performance

- Confirm BBR and BBRv3 appear in available congestion controls and can be
  selected for a test namespace/socket.
- Create/delete representative FQ, CAKE, and PIE qdiscs for Wild/full.
- Exercise IPv4 TTL and IPv6 HL rules, representative IP-set types, and IPv6
  NAT in an isolated test network.
- Compare boot time, suspend drain, thermal behavior, memory pressure, storage
  I/O, and sustained CPU load with stock. Record ambient conditions and avoid
  presenting a single benchmark as a general result.

## Failure capture and rollback

On a failure, stop adding variables. Capture the last successful stage,
`dmesg`, logcat, recovery/bootloader output, module list, and exact reproduction.
Restore the same-OTA stock package/image and confirm the regression disappears.
Sanitize serial numbers, account identifiers, tokens, keys, phone numbers, and
radio/network identifiers before publishing logs.

## Raw partition-image gate

Raw `boot`, `vendor_boot`, `system_dlkm`, `vendor_dlkm`, DTB, or DTBO output
needs a separate record for the exact OTA and region containing partition sizes,
cmdline, bootconfig, DTB/DTBO inputs, module lists, AVB metadata, unpack/repack
results, and byte/semantic round-trip checks. Kernel boot testing alone does
not satisfy this gate.

## Report template

```text
Device / codename:
Region and complete OxygenOS build:
Slot / build fingerprint:
Repository commit / workflow run:
Base / root / feature profile:
Target / optimization / LTO:
Resolved-manifest SHA-256:
Artifact filename / SHA-256:
First boot / cold boot / reboot / suspend:
Root / SUSFS result:
Final-config verification result:
Module vermagic / unresolved symbols / depmod:
Hardware tested (USB IDs and modules):
Networking/performance checks:
Regressions and sanitized logs:
Rollback tested:
Tester and date:
```
