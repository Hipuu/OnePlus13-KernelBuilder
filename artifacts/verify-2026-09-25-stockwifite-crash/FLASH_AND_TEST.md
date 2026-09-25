# On-device handoff — verify the injection reclaim fix (6.6.142 A16)

**Nothing in this file has been run. Every step is yours to execute.**

> **Build status:** run **36125391570** is building `15e5fda` (the bounded-quarantine
> fix). The artifact names below are stable across runs; the sha256s are **not** —
> they will change. Re-read them from the finished run before flashing, or use the
> `gh run download` commands as-is (they fetch by name, not hash).

Run **36115787771** finished green in 25m15s and produced these artifacts (names are exact,
copied from the run — no substitution needed):

    AK3_OP13_A16_android15-6.6.142_KSUN_33239_SuSFS_v2.2.0.zip     (18 MiB)
    kernel_modules_OP13_A16_android15-6.6.142.zip                  (11 MiB)

    gh run download 36115787771 -R Hipuu/OnePlus13-KernelBuilder -n \
        "AK3_OP13_A16_android15-6.6.142_KSUN_33239_SuSFS_v2.2.0.zip"
    gh run download 36115787771 -R Hipuu/OnePlus13-KernelBuilder -n \
        "kernel_modules_OP13_A16_android15-6.6.142.zip"

## Already verified on the build output — you do not need to re-check these

The patch is provably in the shipped driver, checked against the artifacts themselves
(these hashes are from run **36115787771**; the rebuild changes them, so re-check with
`sha256sum` if you want to confirm you flashed the newer build):

- `qca_cld3_peach_v2.ko` exports **12** `hdd_mon_inject_*` symbols, including
  `hdd_mon_inject_tx_complete`, `hdd_mon_hard_start_xmit`, `hdd_mon_get_stats`.
- The 16-bit-lane hardening is in the compiled code: `hdd_mon_inject_tx_complete`
  disassembles to `and w12, w20, #0xffff` at offset `0x24bf14` — the `desc_id & 0xFFFFU` mask.
- The AK3 zip contains `Image`, 38,935,040 bytes,
  sha256 `d1478248de0d0188cde92dac0ca7b91414bfa06ffa9bcda30076c97b12f05c92`.
- `qca_cld3_peach_v2.ko` sha256 `8f1f605b57250645dc64e144279d482e9ec1b714ce36bd5890889b67c59b1df4`.
- vermagic: `6.6.142-android15-8-o-OP-WILD-4k SMP preempt mod_unload modversions aarch64`.

So if the module pack loads at all, it is the patched driver. The remaining question is
only whether the reclaim fix holds under a real attack, which needs the phone.

## What changed in this build versus the one before it

The previous build fixed the token compare (so completions match) and raised the reaper
age to 5000 ms. Both are still in. This build additionally makes the reaper **stop
unmapping**: an aged buffer now moves into a bounded quarantine and its DMA mapping is
released only at vdev teardown, where firmware is provably done with it.

That matters because the previous build's safety rested on the age being long enough,
and an age can only be a heuristic — a host cannot tell "completion lost" from
"completion very late". This build makes firing too early cost a *held mapping* instead
of a use-after-free, so the age is no longer load-bearing.

Concretely, the thing to watch is that the crash does not depend on the age being right:
`reaped` may be non-zero and the run should still survive.

### Proving the quarantine is in the binary you flashed

The previous build was identified by a disassembly offset in
`hdd_mon_inject_tx_complete`. The quarantine functions are `static`, so there is no
symbol to disassemble — use the new log strings instead. They appear nowhere in the
previous build:

    unzip -p kernel_modules_OP13_A16_android15-6.6.142.zip qca_cld3_peach_v2.ko > /tmp/ko
    strings /tmp/ko | grep -c 'into quarantine'      # want 1
    strings /tmp/ko | grep -c 'quarantine full'      # want 2 (the reaper's and the TX gate's)

On-device, the same check against the loaded module:

    adb shell su -c 'strings /sys/module/qca_cld3_peach_v2/sections/.text 2>/dev/null | grep -c quarantine'

If `grep -c hdd_mon_inject /proc/kallsyms` returned 12 but the quarantine strings are
absent, you flashed the previous build — the token fix is in it but not the quarantine.

## Why both artifacts, and why the Image must be flashed

`kernel_modules_*.zip` carries `qca_cld3_peach_v2.ko` — the patched driver. It is built
with `CONFIG_MODVERSIONS=y`, so its symbol CRCs are checked against the **running**
Image. The stock OnePlus kernel's CRCs are not this build's, so the pack will not load
against it. Flash the matching Image first.

(Note: vermagic *branding* is NOT a barrier — `same_magic()` skips the first
space-delimited token under MODVERSIONS, so `-OP-WILD` vs `-Hipuu` is irrelevant. It is
the symbol CRCs that must match. See the `qcacld-module-install` memory.)

## ⚠️ Do NOT use `uname -r` to check the kernel — it is spoofed

This kernel is built with SUSFS, whose `uname()` hook rewrites the release string. On a
correctly flashed device `uname -r` reports a plausible-looking stock value
(`6.6.142-android15-9-g<random>`) that matches **no partition on the device**, and the
random suffix changes between boots. It is not a signal that the flash failed.

The kernel's own banner is not spoofed. Use `/proc/version`:

    adb shell cat /proc/version    # expect: Linux version 6.6.142-android15-8-o-OP-WILD-4k

Or read the value directly:

    adb shell su -c 'cat /proc/sys/kernel/osrelease'   # 6.6.142-android15-8-o-OP-WILD-4k

To confirm independently, read the partition itself — this is ground truth and works
even before a reboot:

    adb shell su -c 'strings /dev/block/by-name/boot_a | grep -m1 "Linux version"'

## Steps

**1. Flash the Image (KernelSU-Next flash mode, no TWRP needed):**

    adb push AK3_OP13_A16_*.zip /data/local/tmp/
    adb shell su -c 'cd /data/local/tmp && unzip -o AK3_OP13_A16_*.zip Image'
    adb shell su -c 'ksud boot-patch --kernel /data/local/tmp/Image --flash -o /data/local/tmp/ak3-out'
    adb reboot

**2. Wait for boot, then confirm the Image is the new one — with `/proc/version`, NOT `uname -r`:**

    adb wait-for-device
    adb shell cat /proc/version   # expect ...6.6.142-android15-8-o-OP-WILD-4k...

**3. Push and load the module pack:**

    adb push kernel_modules_OP13_A16_android15-6.6.142.zip /data/local/tmp/
    adb shell su -c 'cd /data/local/tmp && unzip -o kernel_modules_OP13_A16_android15-6.6.142.zip -d kp'
    adb shell su -c 'cd /data/local/tmp/kp && sh nethunter-wifi.sh conmode monitor 1'

**4. Prove the patched driver is resident (not the stock one):**

    adb shell su -c 'grep -c hdd_mon_inject /proc/kallsyms'     # 0 = stock, 12 = patched
    adb shell su -c 'xxd -p /sys/module/qca_cld3_peach_v2/notes/.note.gnu.build-id'

## The test — STOCK wifite, unmodified

From the Arch container (which shares the host netns), with `wlan0` in monitor mode:

    wifite --bssid <AP> --channel <ch> --no-wps --no-pmkid -p 40

(`-p`/`--pillage` takes the seconds; there is no `-s` flag.)

### What to watch for — the fix is provable from the log

The whole point of this build is that the reclaim path is now **visible**. Watch:

    adb shell su -c 'dmesg | grep -iE "Injection: (session stats|TX complete|reaped|quarantine)"'

| line | healthy | the old crash |
|---|---|---|
| `complete=` | **large** (one per frame) | `0` |
| `reaped=` | **~0** | climbing with every frame |
| `stale=` | some (benign: reused slots) | — |
| `flushed=` | small | — |
| `bssid_skipped=` | **> 0 on a deauth run** | — |

- **`complete > 0` is the fix working.** Before it, the strict token compare could never
  match, so `complete` was structurally 0 (measured 0/235) and `reaped` did all the work
  on a 150 ms timer — which unmapped buffers firmware was still DMA-reading and produced
  the SMMU/NOC fault at 987.9s.
- **`reaped ~= 0` is the healthy case**, but a non-zero `reaped` is no longer fatal in
  this build: those buffers go to the quarantine and their mappings are released at
  teardown instead. If you see `reaped > 0`, look for the follow-on line
  `Injection: reaped N nbuf(s) into quarantine (M held)` — that is the safe path firing,
  and the run should continue normally.
- **`quarantine full` is the one loud failure.** If you see
  `Injection: quarantine full (32 held), refusing TX`, completions are being lost faster
  than the safety net can absorb them and injection has stopped. That is a real bug to
  report (the log line is ratelimited, so it will not flood) — but note it is a *stall*,
  not a crash: no memory is corrupted and a reboot clears it.
- A run that ends `complete=0 reaped>0` has **not** been fixed — report it.
- **`bssid_skipped` is a separate, still-unproven claim.** The driver deliberately refuses
  to create a WMI peer for a frame whose addr1 is a BSSID rather than a station
  (aireplay-ng's directed deauth and ARP replay both emit such frames, and a PEER_CREATE
  for one has hung this firmware). The gate is in the code but has never been observed
  firing on-device — `bssid_skipped=0` in every run so far. A deauth run that reports
  `bssid_skipped=0` means the exclusion did not engage, which is worth reporting even if
  the run otherwise passes. Cross-check that no `PEER_CREATE` was logged for a BSSID:

Also confirm the SMMU/NOC fault did not recur:

    adb shell su -c 'dmesg | grep -icE "SMMU fault|NOC ERROR"'    # want 0

## After the session — reboot, do not switch in place

Leaving monitor mode in place has a documented kernel-panic class (a markerless
timer-UAF ~1 s after `rmmod`), so **reboot** rather than `conmode sta`. Reboot also
restores the stock driver and normal Wi-Fi.

## If it crashes

Capture the whole dmesg and the `Injection:` counter lines before rebooting — with the
new counters, the log alone should say which reclaim path fired and how many times.
The three lines that matter are the `session stats` line, any
`reaped ... into quarantine` line, and any `quarantine full` line; between them they
distinguish "completions were lost but held safely" from "the reclaim path still freed
something firmware held".
