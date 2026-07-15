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
