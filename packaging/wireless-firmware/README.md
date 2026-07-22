# Curated wireless firmware

The builder does not republish the upstream NetHunter release ZIP. It verifies
the pinned `v2.0.6-r1` asset, extracts only the exact members recorded in
`SOURCE-MEMBER-POLICY.json`, adds this curation notice and an expanded
`WIRELESS-FIRMWARE-PROVENANCE.json`, and then creates a deterministic ZIP.

The retained runtime families are:

- ATH9K HTC and the QCA9377 ATH10K USB path;
- the configured MT76 generations;
- the separately built RTW88 driver;
- Realtek firmware requested by the configured USB Bluetooth path; and
- the upstream README and package license, renamed to
  `UPSTREAM-README.md` and `UPSTREAM-PACKAGE-LICENSE.md` so they are not
  mistaken for this bundle's capability or per-blob license policy.

The upstream archive has 274 regular files. The policy retains 65 upstream
members, of which 63 are firmware payloads, and excludes 209. Two byte-identical
root aliases are generated for the MT7662 paths requested by Linux 6.6. The
77-member output also contains this notice, a curated `WHENCE`, seven exact
per-family license/notice files under `LICENSES/`, and the generated provenance
manifest. It contains no executable member and no ELF payload.

In particular, the curation excludes `system/xbin/hid-keyboard`, Magisk
installer/update scripts, GitHub workflow data, HackRF payloads, unrelated
Ralink/Broadcom/ZD1211/RTLWIFI/RTW89/RTL-NIC families, and MediaTek SCP, VPU,
SOF audio and topology content. The pinned source asset has no ATH11K firmware,
MT7603/MT7628 firmware, or matching BCM203x/BFUSB, Broadcom HCD and Intel
Bluetooth payloads, so the bundle does not claim to supply those paths.

The upstream package provides no `WHENCE` file or reliable per-blob license
mapping. This curation independently byte-matches retained files to immutable
linux-firmware snapshots and the locked RTW88 commit, and policy-binds the
exact Qualcomm ATH10K license and QCA9377 firmware-5/firmware-6 notices,
open-ath9k-htc, MediaTek/Ralink and Realtek texts copied into the output. The
open-ath9k-htc notice is stored as LF-safe base64 in this
repository because the pinned upstream text contains a Latin-1 byte; packaging
decodes and verifies its exact original 9,409 bytes. ATH9K HTC is free firmware
with public source; its build was not independently reproduced. ATH10K, MT76,
RTW88 and Realtek Bluetooth remain classified as
`PROPRIETARY-REDISTRIBUTABLE-FIRMWARE`: exact bytes, repositories, commits,
sizes, SHA-256 digests, and exact safe authoritative repository paths are
recorded, but reproducible source is not claimed. The RTW88 repository and
commit are also required to equal the separately locked `rtw88` dependency.
The included upstream MIT text describes the aggregator package and must not
be read as a license assignment for every firmware blob. The dependency lock's
`SEE-CURATION-MANIFEST` classification is validated against the source-member
policy and propagated into the embedded and outer artifact provenance.

Coverage gaps are records in `SOURCE-MEMBER-POLICY.json`, not prose added only
at packaging time. They include ATH11K, MT7603/MT7628, BCM203x/BFUSB, Broadcom
HCD and Intel Bluetooth firmware absent from the pinned source asset, plus the
upstream metadata and reproducibility limitations.

This is a firmware data bundle, not the upstream flashable Magisk module.
Installation must preserve the `system/etc/firmware` relative paths and use a
device-side mechanism whose behavior and rollback path have been reviewed.
