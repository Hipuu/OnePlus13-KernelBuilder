# AnyKernel executable source locations

The `tools/busybox` and `tools/magiskboot` files in this package are
unmodified `arm64-v8a` members extracted from the official Magisk v30.7 APK.
The package records and verifies the APK, member, size, SHA-256 digest, ELF
class, endianness, and machine architecture before creating the ZIP.

Every real package publishes a separate
`OnePlus13-...-corresponding-source.zip` beside the AnyKernel ZIP. It contains
150 checksum-locked source archives: the program roots below, all seven Git
submodules recorded by the Magisk commit, every one of the 140 crates.io
packages in its exact `native/src/Cargo.lock`, and quick-protobuf commit
`980b0fb0ff81f59c0faa6e6db490fb8ecf59c633` used by the locked `pb-rs` and
`quick-protobuf` packages. A canonical `SOURCE-MANIFEST.json` maps every
archive to its source identity, size, SHA-256, license, and binary
relationship. `BUILD-MANIFEST.json` and `SHA256SUMS` seal that companion as a
first-class release artifact.

Corresponding source identities:

- Magisk and magiskboot: `https://github.com/topjohnwu/Magisk.git`, commit
  `e8a58776f1d7bdf852072ad0baa6eceb9a1e4aac` (tag `v30.7`). Clone that exact
  commit. The companion includes its source archive plus the exact `selinux`,
  `lz4`, `libcxx`, `cxx-rs`, `lsplt`, `system_properties`, and `crt0` Git-link
  revisions, so it does not depend on mutable submodule heads. The companion
  also seals the exact 35,178-byte Cargo lock
  (`a04ff0b1edfb97123446dc8c04e44603f772e89ece87967d2b8b291a1bb6d659`)
  and carries every external source archive named by it.
- Magisk BusyBox source: `https://github.com/topjohnwu/ndk-busybox.git`, commit
  `1c0ca97aafb9698ab7770ce1f67af1a84b469cdb`. Magisk's locked build metadata
  downloads `busybox-1.36.1.1.zip` with SHA-256
  `b4d0551feabaf314e53c79316c980e8f66432e9fb91a69dbbf10a93564b40951`;
  its `arm64-v8a/libbusybox.so` is byte-identical to the APK member retained
  here.

The exact GPL-2.0-only BusyBox notice and GPL-3.0-or-later Magisk license are
included under `LICENSES/`. The companion is durable machine-readable source
conveyance for the retained executables and their pinned source submodules.
It is not evidence that this project independently reproduced either binary
byte-for-byte, and it does not claim to archive environment-provided compiler,
Android SDK/NDK, or operating-system toolchains.
