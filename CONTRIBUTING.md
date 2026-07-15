# Contributing

Contributions are welcome when they preserve reproducibility, explicit feature
selection, and OnePlus 13 firmware compatibility.

## Before opening a pull request

1. Create a focused branch from `main`.
2. Keep generated kernel trees and build output under `out/`; do not commit
   source checkouts, proprietary stock images, private signing keys, or modules
   extracted from an OTA.
3. Run the repository validation and the narrowest applicable patch/configure
   checks.
4. For kernel-affecting changes, attach the debug artifact and use the report
   template in [DEVICE-TESTING.md](docs/DEVICE-TESTING.md).

Pull requests run static validation, dry-run the full patch series against all
three source profiles, and compile the OOS 16 full mixed target. A green narrow
job does not substitute for device testing when behavior or boot compatibility
changes.

## Configuration contract

Files ending in `.yml` under `configs/` are intentionally strict JSON text.
This lets the orchestration load them with the Python standard library while
remaining compatible with YAML tooling.

- Device files validate against `schemas/device.schema.json`.
- Release files validate against `schemas/profile.schema.json`.
- Feature files validate against `schemas/feature-profile.schema.json`.
- Every feature profile lists the entire flag catalog. Do not rely on implicit
  inheritance or an omitted flag.
- Add Kconfig changes through a declared fragment. After merging fragments,
  configuration must run `olddefconfig` and verify every `required_symbols`
  entry in the final `.config`.
- A requested patch, symbol, module, checksum, or artifact must fail the build
  when it is missing; do not silently downgrade the selected profile.

When adding a flag, update the feature schema and all three profiles in the same
pull request. Describe the corresponding patch series, Kconfig symbol, runtime
test, and profiles that enable it.

## Dependencies and patches

All network inputs must be declared in `dependencies/lock.yml`.

- Git inputs require a full commit SHA.
- Downloaded files and release assets require SHA-256.
- Mutable branch names may be recorded as upstream context, but the checkout
  must resolve to the committed SHA.
- Do not pipe a network response into a shell.
- Preserve upstream copyright, license, and attribution for every imported
  patch or file.

Ordered patch manifests live under `patches/series/`. Conditions must be
machine-readable and limited to the actual device, base, KMI, root variant, or
feature flag that requires them. Patch failures and unexpected already-applied
states are build failures.

## Module changes

External modules must build against the exact `Module.symvers`, `.config`, and
kernel release produced by the selected manifest. A modules-only build must use
a versioned GitHub artifact from a matching kernel run, never an unkeyed or
best-effort cache. Verify vermagic, unresolved symbols, and `depmod` output.

## Device-test evidence

A boot or hardware-support claim should identify:

- OnePlus model/codename and OxygenOS build/region;
- resolved manifest digest and repository commit;
- root variant, feature profile, optimization, LTO, and build target;
- artifact SHA-256 and resulting `uname -a`;
- boot result, root/SUSFS result, module verification, and relevant hardware
  tests;
- a sanitized `dmesg`/logcat excerpt or debug bundle for any failure.

Do not include device serials, account data, tokens, keys, or complete private
logs in an issue.

## Documentation and commits

Update the README and relevant guide whenever a public input, output, feature
status, or compatibility gate changes. Keep commits small enough to review,
and explain why the change is required rather than only what files changed.
