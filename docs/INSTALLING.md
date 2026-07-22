# Installing a build

Install only a OnePlus 13 artifact whose base profile matches the OxygenOS
version and region currently on the device. A successful CI compile is not
evidence that a different firmware build is compatible.

## Before installation

1. Confirm the device is a OnePlus 13 (`dodge`) and record the complete
   OxygenOS build number and region.
2. Unlock the bootloader using the device/vendor-supported process. Unlocking
   normally erases user data, so finish backups first.
3. Keep the exact stock firmware and stock boot-related images needed for
   rollback. Verify that a host can see the device in both ADB and bootloader
   mode before changing the kernel.
4. Download the complete package set, including the AnyKernel3 ZIP, its
   `-corresponding-source.zip`, `BUILD-MANIFEST.json`, and `SHA256SUMS`, from
   the same workflow run or release.
5. Read the manifest and confirm `device`, `base`, `kmi`, root variant, feature
   profile, builder repository/revision, and resolved-manifest digest.

Verify every downloaded file from the artifact directory:

```bash
sha256sum --check SHA256SUMS
```

On Windows, use a SHA-256 tool that reads the same `SHA256SUMS` entries or
compare `Get-FileHash -Algorithm SHA256` output file by file.

## Kernel package

Use the generated AnyKernel3 ZIP with a maintained flasher/recovery that
supports the device and current firmware. Preserve the package's generated
metadata and installer checks. Do not unpack `Image` and guess a partition or
combine it with DTB, DTBO, vendor boot, or DLKM content from another build.

Reboot once with no optional external hardware attached. If the device reaches
Android, capture the baseline evidence from
[DEVICE-TESTING.md](DEVICE-TESTING.md) before installing optional modules.

Install the manager application that corresponds to the selected root variant.
A build made with `root=none` has no KernelSU interface. OOS 16 plus SUSFS is an
experimental combination and should stay on a device with a tested rollback
path.

## Modules and firmware

Install only a module ZIP whose verified base/root/profile, source manifest,
commit, optimization, LTO, configuration, kernel release, and `Module.symvers`
lineage match the installed kernel. A standalone modules-only run may consume a
prior mixed artifact when that exact lineage matches.

For NetHunter/full builds, verify the separately checksummed wireless firmware
bundle only after the kernel boots normally. It is a deterministic data bundle,
not the upstream flashable Magisk module: inspect
`WIRELESS-FIRMWARE-PROVENANCE.json`, then use a reviewed installation mechanism
that preserves its `system/etc/firmware` relative paths and has a tested
rollback. Reboot, check `dmesg`, then load one driver family at a time. Do not
copy modules from a previous kernel release to make a failed load appear
successful.

## First boot checks

```bash
adb wait-for-device
adb shell uname -a
adb shell cat /proc/version
adb shell getprop ro.build.fingerprint
adb shell dmesg -T | tail -n 200
```

Then verify the selected root manager, SUSFS when enabled, and `modinfo`/
`depmod` results before testing hardware. Sanitize device identifiers before
sharing logs.

## Rollback

If the device does not boot, return to bootloader/recovery using the documented
device key sequence and restore the exact stock image/package for the same OTA
and region. Do not substitute stock images from another OxygenOS revision.

The repository does not publish raw `boot`, `vendor_boot`, `system_dlkm`, or
`vendor_dlkm` images. Their absence is intentional until stock metadata and
round-trip gates are complete.
