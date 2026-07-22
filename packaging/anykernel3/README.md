# AnyKernel3 packaging

The build pipeline checks out AnyKernel3 at the exact commit in
`dependencies/lock.yml`, but it does not copy the generic template wholesale.
The release ZIP contains only this explicit upstream allowlist:

- `LICENSE`;
- `META-INF/com/google/android/update-binary`;
- `META-INF/com/google/android/updater-script`;
- `tools/ak3-core.sh`.

`EXECUTABLE-PROVENANCE.json` binds those four files to their exact Git modes,
byte sizes, SHA-256 digests, and upstream Git blob IDs. Packaging and release
validation recompute each blob from the retained bytes, so a changed and
resealed template helper is rejected even when the outer package checksums are
rewritten.

The generic checkout's BusyBox and magiskboot files are deliberately not
copied. Packaging independently downloads the locked official Magisk v30.7
APK, verifies SHA-256
`e0d32d2123532860f97123d927b1bb86c4e08e6fd8a48bfc6b5bee0afae9ebd5`,
and extracts only `lib/arm64-v8a/libbusybox.so` and
`lib/arm64-v8a/libmagiskboot.so` as `tools/busybox` and
`tools/magiskboot`. Both must be ELFCLASS64, little-endian AArch64
executables and must match the exact sizes and hashes in
`EXECUTABLE-PROVENANCE.json`.

`anykernel.sh` is repository-owned and device-specific. It selects `dodge`,
resolves the active A/B `boot` partition, and performs only `split_boot` plus
`flash_boot`. It contains no inherited Galaxy Nexus block path or Tuna ramdisk
mutation. Raw boot and DLKM partition images are not created by this path.

The two retained ELF tools are necessary for a self-contained AnyKernel3 boot
image repack. `EXECUTABLE-PROVENANCE.json` records their exact APK members,
sizes, SHA-256 digests, source revisions, licenses, ELF identities, and binary
origins. The ZIP member set and every 0644/0755 mode are explicit; packaging
verifies the completed ZIP rather than trusting host filesystem mode bits.
The generic template's unused BusyBox/magiskboot copies, `fec`,
`httools_static`, `lptools_static`, `magiskpolicy`, and
`snapshotupdater_static` prebuilts are excluded.

The package includes the exact upstream GPL-2.0-only and GPL-3.0-or-later
notices under `LICENSES/`, plus `SOURCE-CONVEYANCE.md`. Every real build also
emits a separately checksummed `-corresponding-source.zip` containing 150
checksum-locked archives: exact Magisk and ndk-busybox roots, all seven Magisk
Gitlinks, all 140 registry packages in Magisk's exact `Cargo.lock`, and the
quick-protobuf Git commit used by its two patched Cargo packages. The embedded
policy seals the `Cargo.lock` and `.gitmodules` bytes, Gitlink identities, and
a canonical source manifest. For each of the ten Git archives, the verifier
derives the Git tree from member bytes, modes, symlink targets, recoverable
CRLF export normalization, and the pinned Magisk Gitlinks, then compares that
root to the exact upstream tree object. This proof is independent of the
fail-closed outer archive SHA-256; crates use their `Cargo.lock` checksums. The
companion is stored in canonical path order without recompressing its source
archives. Release validation also requires every ZIP member to carry the
resolved source-epoch timestamp, Unix regular-file 0644 metadata, ZIP version
2.0, and empty extra/comment fields and flags. The retained BusyBox and
magiskboot bytes have durable source and license provenance, but independent
byte-for-byte rebuilds remain to be demonstrated. That status is explicit and
is not implied by the source companion or dependency lock.

No mutable branch checkout is permitted during a release. The generated
release set places `BUILD-MANIFEST.json` and `SHA256SUMS` beside the ZIP so
users can identify and verify the source profile and feature set used to create
it.
