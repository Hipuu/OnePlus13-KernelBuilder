# Dependency lock

`lock.yml` is JSON-compatible YAML so every workflow can validate it with the
Python standard library. Git dependencies are checked out by their full commit
ID; `ref` is either the same immutable commit or an immutable release tag.
Downloaded files must match the recorded SHA-256 before they are executed or
packaged.

## Updating a dependency

1. Review the upstream changes and license.
2. Resolve the desired tag or branch to a full 40-character commit.
3. For a file or release asset, download it to a temporary directory and
   calculate SHA-256 independently.
4. Update the lock and run the repository validation suite.
5. Submit the lock change separately from feature changes when practical.

Release builds reject mutable checkouts, missing digests, abbreviated commits,
and dependencies whose declared purpose does not match the selected profile.
The root MIT license covers this repository's original orchestration code only;
downloaded dependencies and kernel patches retain their upstream licenses.
