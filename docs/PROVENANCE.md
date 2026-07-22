# Release provenance

Every package contains `BUILD-MANIFEST.json` schema version 2. It records the
exact builder revision, selected build tuple, portable paths and SHA-256
digests for the dependency lock and OnePlus manifest, and a deterministically
ordered inventory of every locked dependency. Git dependencies carry full
40-character commits. Downloaded files, archives, and release assets carry
SHA-256 digests; when their source repository is locked too, that source
commit is recorded separately.

The dependency-lock record carries both the byte-for-byte file digest and the
canonical JSON digest used by the pipeline's lineage checks. The release
workflow verifies the package's original `SHA256SUMS` before it creates
release metadata. Its checked-in provenance generator then compares those
digests and the dependency inventory with the files in the exact checked-out
orchestrator revision, and rejects a
package when any of these values differs from the release request:

- orchestrator repository or commit;
- base, root variant, feature profile, build target, optimization, or LTO;
- debug/pre-release state, branding, or an explicitly requested timestamp;
- resolved-manifest, locked-manifest, or dependency-lock digest;
- OnePlus manifest repository revision; or
- an immutable dependency identity.

## CI validation tooling

Workflow and shell linting do not use mutable tools from the hosted-runner
image. `dependencies/lock.yml` pins Actionlint 1.7.11 and ShellCheck 0.11.0 by
release tag, source commit, asset size, and SHA-256. Repository-owned launchers
also require the exact archive member set, order, modes, inner executable size,
and inner executable digest before running either binary. Validation therefore
fails when a runner-provided version changes or either release archive differs.

## Wireless firmware curation

NetHunter/full packaging does not copy the upstream wireless release ZIP. The
builder verifies the locked asset SHA-256 and source commit, validates every
retained member against the repository-owned
`packaging/wireless-firmware/SOURCE-MEMBER-POLICY.json`, and produces a new
deterministic ZIP. Its exact allowlist contains ATH9K HTC, ATH10K, configured
MT76, RTW88 and USB Bluetooth firmware. The upstream README and package-level
MIT text are retained as `UPSTREAM-README.md` and
`UPSTREAM-PACKAGE-LICENSE.md`, names that do not imply driver coverage or a
blanket firmware license.

The pinned source archive has 274 regular files. The curation retains 65
upstream members, including 63 firmware payloads, and excludes 209. Two
byte-identical MT7662 root aliases are then generated for the Linux 6.6 request
paths. The output has 77 members after adding those aliases, the repository
curation notice, curated `WHENCE`, seven exact family license/notice texts, and
`WIRELESS-FIRMWARE-PROVENANCE.json`. Packaging
rejects a missing or changed allowlisted member, a new or changed source ELF,
an undeclared output member, any output ELF, an executable output mode, or a
policy identity that differs from `dependencies/lock.yml`.

The embedded provenance expands every retained policy record with its path,
size, SHA-256, family, source asset URI, source repository, exact safe
repository-relative path, release tag and commit, lock and family license
classifications, and provenance status. The corresponding
artifact record in `BUILD-MANIFEST.json` records the policy and embedded
manifest digests, source/retained/excluded counts and per-family counts.

The upstream package supplies neither ATH11K firmware nor a `WHENCE` file with
per-blob license assignments. The curation independently maps exact bytes to
immutable linux-firmware snapshots and the locked RTW88 commit. Its curated
`WHENCE` carries the authoritative snapshot, license-file URIs and digests;
the policy also binds and packages the exact Qualcomm ATH10K license plus
QCA9377 firmware-5/firmware-6 notices,
open-ath9k-htc, MediaTek/Ralink, and Realtek texts under `LICENSES/`. The
Latin-1 open-ath9k-htc notice uses a base64 repository transport so checkout
line-ending or encoding conversion cannot alter the 9,409 packaged bytes.
ATH9K HTC is free, source-available firmware, though an independent
byte-reproducible build has not been established. ATH10K, MT76, RTW88 and
Realtek Bluetooth remain proprietary redistributable firmware whose exact
bytes are pinned; the project does not claim reproducible firmware source. The
curation specifically removes the upstream HID executable and non-wireless
MediaTek SCP, VPU, SOF audio and topology content. Coverage gaps are validated
policy records, including missing ATH11K, MT7603/MT7628, BCM203x/BFUSB,
Broadcom HCD, and Intel Bluetooth payloads; they are copied verbatim into the
embedded provenance rather than synthesized by the packager.

## AnyKernel executable curation

AnyKernel packaging copies only the pinned template's license, updater entry
files, and source-visible core shell helper. It does not reuse the template's
ARM32 tools. The builder fetches the separately locked official Magisk v30.7
APK, verifies SHA-256
`e0d32d2123532860f97123d927b1bb86c4e08e6fd8a48bfc6b5bee0afae9ebd5`,
and extracts only its `arm64-v8a` BusyBox and magiskboot members. Their exact
sizes and SHA-256 digests are checked, and their ELF headers must report
ELFCLASS64, little-endian, ET_EXEC, and EM_AARCH64.

`EXECUTABLE-PROVENANCE.json` binds the AnyKernel and Magisk dependency
identities, APK members, executable origins, source revisions, license files,
the release asset's `SEE-UPSTREAM-MULTIPLE` classification, and explicit
not-yet-byte-reproducible status. Exact GPL-2.0-only and
GPL-3.0-or-later texts plus `SOURCE-CONVEYANCE.md` are packaged alongside the
tools. Its `template_members` contract binds exactly four AnyKernel upstream
files to their Git modes, byte sizes, SHA-256 digests, and Git blob objects:
`LICENSE` (`20a447ebe28baf309eeed88eac8cd86a4c3eeeec`),
`META-INF/com/google/android/update-binary`
(`8c7006e7e3f6ef10f8f4117b291d6df204ef285e`),
`META-INF/com/google/android/updater-script`
(`8f5b52376c03dfa0b3f61446a830ecca9e8a03cc`), and `tools/ak3-core.sh`
(`43baccb2b6b1febf4815bc9f74f81da7d72db61d`). Packaging and release
validation both recompute those blob objects from the packaged bytes. The
completed ZIP is also verified against a fixed member set and fixed 0644/0755
mode map, so output permissions do not depend on host filesystem semantics.

Every non-smoke AnyKernel build also emits a separately checksummed
`-corresponding-source.zip`. Its repository-owned policy and dependency lock
bind 150 immutable source archives by origin, byte size, and SHA-256: the
Magisk release source, the exact BusyBox source named by the retained
executable contract, all seven Gitlinks recorded by the Magisk commit, all 140
checksum-locked crates.io packages in Magisk's exact Cargo lock, and one
quick-protobuf Git archive supplying its two patched Cargo packages. Packaging
seals and reopens the exact 35,178-byte `Cargo.lock`, verifies its complete
155-package inventory against those archives, and validates the exact
`.gitmodules` bytes and Gitlink repository/path/commit map. Each source archive
must be a bounded, single-root tree without unsafe members, POSIX path escapes,
or Windows drive-qualified members or links.

Every Git-backed source record additionally pins the upstream Git tree object.
For all ten Git archives, validation reconstructs the Git tree directly from
the tar members: regular-file bytes and executable modes become Git blobs,
symlink targets become `120000` blobs, applicable `eol=crlf` exports are
normalized back to repository LF bytes, and the seven pinned Magisk Gitlinks
are inserted as `160000` entries. It then hashes the resulting Git blob/tree
objects and requires the derived root to equal the policy's exact tree ID.
Hardlinks, unsupported member types, and archive attributes whose repository
bytes cannot be recovered fail closed. The archive SHA-256 remains a separate
cache identity, while the derived tree proof preserves durable commit-content
identity if a hosting provider regenerates outer archive bytes. Registry
crates use the checksum recorded by `Cargo.lock` and do not claim a Git tree.

The 150 source archives, `SOURCE-POLICY.json`, and a canonical
`SOURCE-MANIFEST.json` are written to a deterministic `ZIP_STORED` companion,
avoiding zlib-version-dependent recompression of already compressed inputs.
The manifest maps both retained executables to their complete source closure
and records the exact dependency-lock digest. This is complete source
conveyance for the retained upstream program inputs; it deliberately does not
claim an independent byte-for-byte rebuild or bundle external Android
SDK/compiler toolchains.

Release provenance generation independently reopens this companion before
publication. It requires the exact checked-out source policy, canonical member
order, stored compression, and regular-file 0644 modes. Every member must use
the resolved source epoch at ZIP's two-second timestamp precision, Unix
creator metadata, ZIP version 2.0, empty extra/comment fields, and zero flags,
volume, and internal attributes; the archive comment must also be empty.
Release validation verifies every embedded archive against both the policy and
dependency lock, opens all 150 inner source archives, rechecks safe roots and
Cargo manifest/license identities, and reconciles the embedded `.gitmodules`
and `Cargo.lock` closure. It binds
`SOURCE-MANIFEST.json` to the lock digest and retained executable hashes, and
rejects a missing, added, or changed release asset even if an outer checksum
file was rewritten. The original package
`SHA256SUMS` must be the complete canonical inventory represented by
`BUILD-MANIFEST.json` before in-toto provenance and release checksums are
generated.

## Build toolchain gate

Immediately after locked source synchronization, and before patching,
configuration, or compilation, the workflows run
`scripts/record-build-toolchain.py`. The gate reads the single
`CLANG_VERSION` selected by
`kernel_platform/common/build.config.constants` and verifies every core tool
used by the official `LLVM=1`, `LLVM_IAS=1` setup:

- `clang` and `clang++`;
- `ld.lld`; and
- `llvm-ar`, `llvm-nm`, `llvm-objcopy`, `llvm-objdump`, `llvm-readelf`,
  `llvm-size`, and `llvm-strip`.

The same bounded gate follows the official Bazel launcher chain rather than
searching the checkout for executables. It records
`kernel_platform/tools/bazel`, its OnePlus `bazel.sh` wrapper,
`bazel.origin.sh`, `gettop.sh`, the pinned build-tools Python interpreter,
`bazel.py`, and the terminal
`prebuilts/kernel-build-tools/bazel/linux-x86_64/bazel` binary. The symlink
target and source markers selecting each next stage are fail-closed. Every
component carries its owning resolved-manifest project and commit, digest,
size, kind, and nearest metadata; the pinned Python and Bazel executables also
carry their `--version` output.

The gate fails when the declaration or a tool is missing, a resolved path
leaves the synchronized source tree, a tool is not owned by the exact
`kernel_platform/prebuilts/clang/host/linux-x86` project, or either the
declaration or tool project lacks its pinned resolved-manifest commit.
Symlinked aliases are recorded by both selected and canonical path and are
accepted only when the canonical target remains in that pinned project. Before
any recorded executable is probed, both paths are compared directly with the
declared resolved-manifest commit: checkout HEAD, Git tree path, blob object,
regular/executable/symlink mode, and (for links) the literal target must all
match. Dirty bytes, modes, link targets, missing objects, and a checkout at a
different commit fail the gate. Bazel chain components likewise must remain in the root,
`prebuilts/build-tools`, or `prebuilts/kernel-build-tools` manifest project
declared for that stage.

`build-toolchain-provenance.json` is written identically to the debug
directory and the source `.op13` metadata directory. It records deterministic
source-relative paths, manifest project and commit, byte size, SHA-256,
version output, ELF/script kind, and the nearest license/notice and build
metadata found by a bounded ancestor search. It does not recursively hash the
toolchain tree. Host Bash, Git, Python, Make, the runner/storage utilities, and
the core archive/module tools used by the pipeline are recorded in a separate
`host_environment_tools` array as mutable `environment-provided` inputs. The
GitHub `ImageOS` and `ImageVersion` values are recorded separately as mutable
runner-image evidence. Neither paths nor versions in those host records are
presented as pinned manifest artifacts.

After the real kernel build, the toolchain record and strict core/feature KMI
stamps are validated against their source postimages and copied into the
reusable kernel metadata. Packaging validates their sealed sizes and SHA-256
digests again, publishes the applicable records as release files, includes them
in the debug ZIP, and records portable hash/lineage entries in
`BUILD-MANIFEST.json`. Release
provenance generation revalidates the packaged files before they become in-toto
subjects. The reusable kernel archive omits only the two disposable Kconfig
work trees, not these evidence records.

Before the official real build starts, `build.py` verifies that the resolved
manifest is a plain file inside the synchronized source root, still matches its
sealed SHA-256 and carries the selected profile's immutable URL, filename and
40-hex revision identity. It then exports the exact
`KLEAF_REPO_MANIFEST=<source-root>:<resolved-manifest>` binding; it never points
Kleaf at a mutable `.repo` or runner path. The packaged evidence stores the
same binding as source-relative POSIX fields plus its manifest digest. This SCM
identity is kept distinct from the reproducible time input: `SOURCE_DATE_EPOCH`
continues to resolve from the exact common-kernel commit unless an explicitly
validated timestamp was selected.

For hosted OOS 16 compilation, the repository fixes
`EXTRA_KBUILD_ARGS` to
`--jobs=2 --local_cpu_resources=2 --local_ram_resources=6144`. The value is not a
workflow input: `build.py` derives it only from the `oos16` profile plus the
GitHub Actions environment and overwrites any inherited value before invoking
the official OnePlus script. OOS 15 and local builds explicitly pass an empty
value and retain the upstream tool default. The selected policy, job count, CPU
and RAM resource values, and 8192-MiB hosted swap contract are sealed in
`kernel.resource_policy`, so they travel through the debug bundle and
`BUILD-MANIFEST.json`.

## Vendor-module KMI closure

The locked OnePlus builds use strict GKI symbol-list enforcement. The configured
vendor-module closure requires `from_kuid` for
`oplus_bsp_mm_osvelte.ko` and `from_kuid_munged` for `msm_sysstats.ko`.
Before feature patches, `scripts/integrate-kmi-symbol-requirements.py` adds only
those two exports to the profile's OnePlus and Qualcomm lists.

Every OOS 15 China, OOS 15 global and OOS 16 list has an exact preimage and
postimage size and SHA-256 contract. All contracts are validated before either
file is changed, the writes are atomic with rollback on a write failure, and
the operation is idempotent only for the exact recorded postimages. Any other
source state fails the patch phase. The resulting
`.op13-kmi-symbol-exports.json` records the base, consumers, symbols, hashes and
strict-mode status in the synchronized common tree.

When and only when the sealed feature selection enables
`nethunter.wifi_ath`, the immediately following wireless closure adds
`__ieee80211_get_radio_led_name` for `ath9k.ko`/`ath9k_htc.ko` and
`__ieee80211_create_tpt_led_trigger` for
`ath9k.ko`/`ath9k_htc.ko`/`mt76.ko`. Evidence validation requires those exact
two ordered records and consumer sets, then proves the core Qualcomm postimage
is the wireless preimage and the live list is the recorded wireless postimage.
A missing required stamp or a stale stamp while the feature is disabled fails
closed. Immediately after patching, both applicable stamps are validated and
copied to source metadata and `out/debug`; therefore configuration or compile
failure still retains the KMI evidence. Successful builds carry the same files
through the reusable archive, package, debug ZIP, and release provenance.

For classic KernelSU at commit
`b0bc817b4e966aa6aa830834eaf6ef765d821d40`, the SUSFS compatibility operation
changes only the two direct wrapper calls whose function-address guards are
always true. Both edits are constrained by exact KernelSU/SUSFS
`a8c720c42ca46fca13179280b13aa13c9fbe1562` pre/postimage hashes; compiler
warnings remain globally enabled.

## in-toto statement

`provenance.intoto.jsonl` is a canonical, single-line in-toto Statement using
the `https://slsa.dev/provenance/v1` predicate. Its subjects are every rebuilt
release asset except the statement itself and the release checksum file. Its
sorted `resolvedDependencies` include:

- the exact orchestrator commit;
- `dependencies/lock.yml` and its SHA-256 digest;
- the selected repository-owned OnePlus lockfile and its digest;
- the exact `repo manifest -r` output and its digest;
- every Git or downloaded dependency in the lock, including separate source
  commits for pinned release assets where available; and
- every project/commit pair parsed from the resolved OnePlus manifest.

Project descriptors retain the manifest project name and checkout path as
annotations. This makes the kernel, vendor tree, modules/device-tree projects,
toolchains, and other OnePlus manifest inputs independently visible to
provenance consumers instead of hiding them behind only the orchestrator SHA.

`RELEASE_SHA256SUMS` covers the statement plus every other release file and is
written in path order. The workflow verifies it before publication. GitHub's
OIDC-backed build-provenance action then creates a separate platform
attestation for the published asset set; the publish job does not persist
checkout credentials and is the only job with `contents: write`.

## Verification

After downloading the complete release asset set, verify the release checksums
from that directory:

```bash
sha256sum --check RELEASE_SHA256SUMS
```

Inspect `BUILD-MANIFEST.json` for the selected device/build tuple and inspect
`predicate.buildDefinition.resolvedDependencies` in
`provenance.intoto.jsonl` for the exact manifest projects and external
dependencies. GitHub's attestation verification command can then verify the
platform attestation against `Hipuu/OnePlus13-KernelBuilder`.

Provenance proves build inputs and artifact identity. It does not replace the
physical boot, module-load, hardware, or raw-image round-trip gates documented
in the device test protocol.
