# On-device handoff — verify the injection reclaim fix (6.6.142 A16)

**Nothing in this file has been run. Every step is yours to execute.**

Run **36112030043** finished green in 21m3s and produced these artifacts (names are exact,
copied from the run — no substitution needed):

    AK3_OP13_A16_android15-6.6.142_KSUN_33239_SuSFS_v2.2.0.zip     (18 MiB)
    kernel_modules_OP13_A16_android15-6.6.142.zip                  (11 MiB)

    gh run download 36112030043 -R Hipuu/OnePlus13-KernelBuilder -n \
        "AK3_OP13_A16_android15-6.6.142_KSUN_33239_SuSFS_v2.2.0.zip"
    gh run download 36112030043 -R Hipuu/OnePlus13-KernelBuilder -n \
        "kernel_modules_OP13_A16_android15-6.6.142.zip"

## Already verified on the build output — you do not need to re-check these

The patch is provably in the shipped driver, checked against the artifacts themselves:

- `qca_cld3_peach_v2.ko` exports **12** `hdd_mon_inject_*` symbols, including
  `hdd_mon_inject_tx_complete`, `hdd_mon_hard_start_xmit`, `hdd_mon_get_stats`.
- The 16-bit-lane hardening is in the compiled code: `hdd_mon_inject_tx_complete`
  disassembles to `and w12, w20, #0xffff` — the `desc_id & 0xFFFFU` mask.
- The AK3 zip contains `Image` (38,935,040 bytes), so the flash command below is correct.
- vermagic: `6.6.142-android15-8-o-OP-WILD-4k SMP preempt mod_unload modversions aarch64`.

So if the module pack loads at all, it is the patched driver. The remaining question is
only whether the reclaim fix holds under a real attack, which needs the phone.

## Why both artifacts, and why the Image must be flashed

`kernel_modules_*.zip` carries `qca_cld3_peach_v2.ko` — the patched driver. It is built
with `CONFIG_MODVERSIONS=y`, so its symbol CRCs are checked against the **running**
Image. The phone currently runs the stock OnePlus kernel (`6.6.142-android15-9-g57394986`,
built Aug 3), whose CRCs are not this build's, so the pack will not load against it.
Flash the matching Image first.

(Note: vermagic *branding* is NOT a barrier — `same_magic()` skips the first
space-delimited token under MODVERSIONS, so `-OP-WILD` vs `-Hipuu` is irrelevant. It is
the symbol CRCs that must match. See the `qcacld-module-install` memory.)

## Steps

**1. Flash the Image (KernelSU-Next flash mode, no TWRP needed):**

    adb push AK3_OP13_A16_*.zip /data/local/tmp/
    adb shell su -c 'cd /data/local/tmp && unzip -o AK3_OP13_A16_*.zip Image'
    adb shell su -c 'ksud boot-patch --kernel /data/local/tmp/Image --flash -o /data/local/tmp/ak3-out'
    adb reboot

**2. Wait for boot, then confirm the Image is the new one:**

    adb wait-for-device
    adb shell uname -r          # expect this build's version, not ...-9-g57394986

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

    adb shell su -c 'dmesg | grep -iE "Injection: (session stats|TX complete|reaped|stale)"'

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
- **`reaped ~= 0` is the crash being gone.** If `reaped` climbs, completions are being
  lost and `HDD_MON_INJECT_AGE_MS` needs raising.
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
