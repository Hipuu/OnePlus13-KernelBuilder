# Security policy

## Reporting a vulnerability

Use GitHub's private vulnerability reporting for
`Hipuu/OnePlus13-KernelBuilder` when the report involves the build pipeline,
release provenance, credential handling, a malicious dependency path, or a
kernel change with security impact. Include the affected commit or release,
reproduction details, impact, and any suggested remediation. Avoid opening a
public issue until the report has been assessed.

Ordinary build failures, feature requests, and device compatibility reports
belong in the public issue templates.

## Supported versions

Security fixes are made on the current `main` branch and, when appropriate, in
a replacement release. Older artifacts are not rebuilt automatically. Verify
the repository commit, `BUILD-MANIFEST.json`, and `SHA256SUMS` before installing
an artifact.

## Credentials

- Do not place GitHub tokens, signing keys, SSH keys, OTA decryption material,
  or device secrets in source files, workflow inputs, issue logs, cache keys,
  artifact names, or remote URLs.
- GitHub Actions should use `${{ github.token }}` with job-scoped permissions.
- Release publishing is the only workflow operation that needs
  `contents: write`; cache cleanup is the only operation that needs
  `actions: write`.
- If a credential appears in a task, log, commit, or artifact, revoke it first,
  remove it from every retained copy, and review its audit history before
  continuing.

## Supply-chain policy

Every Git dependency is pinned to a full commit. Every downloaded file or
release asset is verified by SHA-256. Resolved repo manifests and build context
are retained with build artifacts. Workflow actions are pinned to full commit
SHAs, and release jobs rebuild from locked inputs.

Network-fetched setup scripts are not executed directly. Dependency changes
must be reviewed together with their upstream license, commit diff, checksum,
and reason for use.

## Artifact trust boundary

This project changes privileged kernel code. A successful compile proves build
consistency, not device safety. Treat artifacts as untrusted until the matching
profile completes the test protocol in [DEVICE-TESTING.md](docs/DEVICE-TESTING.md).
Never install an artifact whose device, region, OxygenOS base, KMI, or checksum
does not match its recorded build manifest.

The project does not distribute stock OnePlus images, proprietary modules, or
private signing keys. Raw partition-image generation remains disabled until
exact stock metadata and round-trip tests are available for the same OTA and
region.
