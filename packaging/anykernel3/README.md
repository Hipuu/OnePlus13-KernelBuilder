# AnyKernel3 packaging

The build pipeline checks out the AnyKernel3 dependency at the exact commit in
`dependencies/lock.yml`, verifies the checkout, and copies only the resulting
kernel `Image` plus generated device metadata into a temporary packaging tree.

No mutable branch checkout is permitted during a release. The generated
release set places `BUILD-MANIFEST.json` and `SHA256SUMS` beside the ZIP so
users can identify and verify the source profile and feature set used to create
it.

Raw boot or DLKM partition images are not created by this packaging path.
