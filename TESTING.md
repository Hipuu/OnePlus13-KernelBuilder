# Testing and Debugging

## Local static validation

Before publishing workflow changes, run the bundled validator:

```bash
./validate_workflow.sh
```

It checks that:

- Every workflow and composite `action.yml` parses as YAML, and all six device configs / six manifests parse as JSON / XML.
- `actionlint` reports no problems (skipped with a notice if not installed). One rule is suppressed: actionlint through 1.7.7 still enforces a 10-input cap on `workflow_dispatch`, but GitHub's documented limit is 25 top-level inputs, and this workflow's 16 dispatch fine.
- The workflow has a `workflow_dispatch` trigger and every action declares `name`, `description`, and `runs`.
- All nested local composite-action references resolve to a real `action.yml`.
- Every `configs/OP13-*.json` targets `wild/sm8750` and names a manifest that exists at `manifests/<os_version>/<manifest>`.
- The reference manifest pins the common kernel, Clang, kernel build-tools and WildKernels AnyKernel3 to full 40-character SHAs. All six manifests share identical toolchain pins.
- No `mkbootimg`, `avbtool`, `boot.img`, `vendor_boot`, `vendor_dlkm`, or `system_dlkm` build command exists. (`kernel_modules` paths are permitted — those are loadable `.ko` packages, not images.)
- The exact pinned toolchain-cache assets are reachable in the public WildKernels `toolchain-cache` release.
- Every file the workflow needs is tracked by git, so a fresh runner checkout has it. New configs and manifests must be `git add`-ed before this passes.

Set `SKIP_NETWORK=1` to skip the asset-reachability check when offline.

## Live workflow commands

Check recent runs:

```bash
gh run list -R Hipuu/OnePlus13-KernelBuilder --workflow "Build OnePlus 13 Kernel" --limit 5
```

Watch a run:

```bash
gh run watch RUN_ID -R Hipuu/OnePlus13-KernelBuilder
```

Read failed-step logs:

```bash
gh run view RUN_ID -R Hipuu/OnePlus13-KernelBuilder --log-failed
```

Download successful artifacts:

```bash
gh run download RUN_ID -R Hipuu/OnePlus13-KernelBuilder -D artifacts
```

## Build profiles

Full default build (6.6.89 A16, KSUN + SUSFS + Nethunter + wireless modules):

```bash
gh workflow run "Build OnePlus 13 Kernel" -R Hipuu/OnePlus13-KernelBuilder
```

Single non-default variant:

```bash
gh workflow run "Build OnePlus 13 Kernel" -R Hipuu/OnePlus13-KernelBuilder -f kernel_version="6.6.118 A16"
```

All six variants in parallel:

```bash
gh workflow run "Build OnePlus 13 Kernel" -R Hipuu/OnePlus13-KernelBuilder -f kernel_version=all
```

Minimal diagnostic build — no SUSFS, no optimization patches, no Nethunter, no modules. Use this to isolate whether a failure comes from the base pipeline or from the added feature sets:

```bash
gh workflow run "Build OnePlus 13 Kernel" -R Hipuu/OnePlus13-KernelBuilder -f use_susfs=false -f use_opt_patches=false -f nethunter=false -f wireless_modules=false -f optimize_level=O2 -f lto=thin
```

Original KernelSU build:

```bash
gh workflow run "Build OnePlus 13 Kernel" -R Hipuu/OnePlus13-KernelBuilder -f ksu_variant=KSU -f ksu_branch=main
```

Publish a prerelease of every variant:

```bash
gh workflow run "Build OnePlus 13 Kernel" -R Hipuu/OnePlus13-KernelBuilder -f kernel_version=all -f release_type=prerelease
```

## Job graph

| Job | Runs when | Purpose |
|-----|-----------|---------|
| `plan` | always | Maps the `kernel_version` choice to a JSON matrix of config paths (`all` expands to all six) |
| `build` | after `plan` | One runner per variant, `fail-fast: false` so one bad variant does not cancel the rest |
| `firmware` | `wireless_modules=true` | Re-publishes the latest `nullptr-t-oss/Nethunter-Wireless-Firmware` release asset |
| `release` | `release_type != none` and `build` succeeded | Downloads every artifact, flattens them, and publishes one aggregate release |

Release creation is a single aggregate job on purpose: a per-matrix-job release step would race when several variants finish at once.

## Expected stages within `build`

1. Checkout the builder.
2. Install dependencies and configure ccache.
3. Load the matrix's OnePlus 13 configuration and resolve the KernelSU ref to a concrete commit SHA.
4. Download pinned source/toolchain archives from the manifest.
5. Add KernelSU / KernelSU-Next.
6. Apply SUSFS (optional), HMBIRD, module-overlay, optimization, networking, NTSync and other configured patches.
7. Apply Nethunter inline configs (Bluetooth, SDR, CAN, USB serial) when `nethunter=true`.
8. Clone the pinned out-of-tree `lwfinger/rtw88` tree and append the wireless `=m` configs when `wireless_modules=true`.
9. Generate `gki_defconfig`, apply O2/O3 and LTO choices, and compile `Image` (plus `modules` / `modules_install` when modules are enabled).
10. Package the WildKernels AnyKernel3 ZIP, then the kernel-modules ZIP.
11. Upload the AK3 ZIP and modules ZIP (from the composite action), the raw `Image`, and optional debug artifacts.

## Expected artifacts

- `AK3_<MODEL>_<OS>_<KERNEL>_<KSU>_<VER>.zip` — flashable AnyKernel3 package.
- `kernel_modules_<MODEL>_<OS>_<KERNEL>.zip` — wireless/CAN loadable modules, plus a flattened `modules.dep` and the `nethunter-wifi.sh` loader, when `wireless_modules=true`.
- `Nethunter-Wireless-Firmware-<VER>.zip` — firmware passthrough, when `wireless_modules=true`.
- `Image_<MODEL>_<KERNEL>` — raw ARM64 kernel `Image`.
- Debug artifact(s) when `debug=true`.

The workflow intentionally does not create boot, vendor boot, or DLKM images.

### Artifact upload details

The AK3 and modules ZIPs are uploaded with `archive: false` so GitHub serves the ZIP we built rather than a ZIP-of-a-ZIP. Two consequences:

- The `release` job must use `actions/download-artifact@v8`. v7 unconditionally unzips and would corrupt these downloads; v8 checks `Content-Type` first.
- The raw `Image` is uploaded *archived* on purpose. An extensionless `archive: false` upload is served as `application/octet-stream`, and browsers save it as `image.bin`.

## Verifying a module pack on device

Modules are built with `CONFIG_MODVERSIONS=y`, so a `kernel_modules_*.zip` loads **only** on the exact kernel build it shipped with — matching base versions is not enough. To sanity-check a pack:

```bash
insmod ./cfg80211.ko
```

If that fails with a version-magic or unknown-symbol error, the pack does not match the running kernel. If it succeeds, load `mac80211.ko` next, then the driver chain. See the README's "Using wireless modules" section for the full unload/load procedure.

In practice, use the bundled loader instead of doing that by hand:

```bash
./nethunter-wifi.sh            # interactive menu
./nethunter-wifi.sh load       # ath9k_htc by default
./nethunter-wifi.sh status
./nethunter-wifi.sh restore
```

To put the *internal* Wi-Fi into monitor mode (no external adapter needed):

```bash
./nethunter-wifi.sh conmode monitor 6
tcpdump -i wlan0 -e -nn
./nethunter-wifi.sh conmode sta
```

### Testing over wireless ADB

Loading any `mac80211` driver displaces the internal Wi-Fi — including the link carrying an `adb connect` session. Test scripts must therefore be self-contained and detached, and must restore the stack themselves:

```bash
adb -s <ip>:5555 shell 'su -c "setsid /data/local/tmp/test.sh"'
```

Three things that cost time here:

- `setsid`, not `nohup`. `nohup` tries to create `nohup.out` in the cwd, which is `/` and read-only, so it exits before running anything. Redirect inside the script with `exec >"$LOG" 2>&1` instead.
- Set `persist.adb.tcp.port 5555` before starting, so the daemon still listens on TCP after the reboot that a failed restore forces.
- **The device usually returns on a different IP.** Each restore/reboot cycle took a new DHCP lease in testing (`.2` → `.22` → `.24` → `.26`), so poll a port sweep rather than reconnecting to the old address:

```bash
for n in $(seq 2 60); do timeout 1 bash -c "echo > /dev/tcp/192.168.1.$n/5555" 2>/dev/null && echo "$n"; done
```

Note that `adb connect` to a dead address blocks ~20s on TCP timeout, so probe the port first — a bare reconnect loop will exceed the tool timeout long before the device is back.

### Checking the packaging step

The `Create Kernel Modules ZIP` step asserts that `modules.dep` has exactly one line per packaged module and fails the build otherwise. Confirm in the log:

```
After dependency closure: 77
modules.dep entries: 77
```

A downloaded pack should contain `<n>` `.ko` files plus exactly two extra entries (`modules.dep`, `nethunter-wifi.sh`).

### Checking the loader without a device

`validate_workflow.sh` covers the loader statically: POSIX syntax via `dash -n`, no bashisms or `awk` (Android's toybox has neither), LF line endings, and the `#!/system/bin/sh` shebang. A CRLF script does not execute on Android at all — the kernel reads the shebang as `/system/bin/sh\r` and reports "No such file or directory". `.gitattributes` pins `*.sh` to `eol=lf` to prevent that on Windows checkouts.

The dependency walk can be exercised off-device by sourcing its functions against a pack's real `modules.dep` and stubbing `insmod`/`resident`, then asserting that no module is loaded before one of its dependencies. Worth re-running after any change to `load_closure`, `closure_of`, or the packaging `awk`: the deep chains (`rtw_8723cs`, `mt7921u`) and the dashed module names (`mt76-usb`, `crc-itu-t`) are where ordering bugs surface.

## Historical failures

The original hand-written action failed for several independent reasons: Ubuntu 24.04 package names, incorrect OnePlus manifest branch/name, incorrect `common/` path, invalid YAML from a heredoc, guessed SUSFS/HMBIRD paths, wrong KernelSU checkout directory and use of the generic osm0sis AnyKernel3 template. That action was removed and replaced with the pinned WildKernels pipeline; these old run IDs are not representative of the current implementation.

A later failure came from letting KernelSU-Next's `setup.sh` pick its own ref: with no argument it checks out the latest *tag*, whose `kernel/Kconfig` predates what `10_enable_susfs_for_ksu.patch` expects, leaving an unfixable `kernel/Kconfig.rej`. The workflow now resolves the ref to a commit SHA before building.
