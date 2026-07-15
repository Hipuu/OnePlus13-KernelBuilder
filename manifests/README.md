# Locked OnePlus manifests

The files in `lockfiles/` are derived from the official OnePlus SM8750
manifest repository at commit
`f8e50677874c65b6da41057d2f39be7b4ef3c08a`. Every project revision in a
locked manifest is a full commit ID. In particular, the three OnePlus projects
that use moving branches upstream are resolved before the lock is committed.

Normal and release builds initialize `repo` from the pinned manifest repository
and then sync the matching local lockfile. After sync, the pipeline emits a
fresh `repo manifest -r` and compares its project names, paths, remotes, and
revisions with the committed lock.

The source monitor may report upstream drift, but it never edits these files or
publishes a build automatically. Updating a lock requires review, patch dry-run,
a full compile, and device validation on the corresponding firmware profile.
