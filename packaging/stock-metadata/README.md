# Stock partition metadata gate

Raw `boot`, `vendor_boot`, `system_dlkm`, `vendor_dlkm`, DTB, or DTBO images may
only be produced when a metadata document for the exact OnePlus OTA build and
region validates against `schema.json` and has both round-trip gates enabled.

Metadata must be derived from user-supplied stock firmware. Do not copy
partition metadata or proprietary modules from a different device, region, or
firmware release. The repository intentionally contains no stock images,
private keys, or proprietary modules.

Until verified metadata is committed for a profile, workflows publish the
kernel Image, AnyKernel3 package, external module bundle, checksums, and debug
metadata only.
