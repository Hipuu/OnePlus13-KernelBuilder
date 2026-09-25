# Stock wifite crashes the injection path — 2026-09-25

**Method:** STOCK wifite 2.8.1/2.8.2 (unmodified, container at
`/usr/lib/python3.14/site-packages/wifite`), invoked as:

    wifite --bssid d4:9a:a0:c9:54:10 --channel 1 --no-wps --no-pmkid -p 40

No wifite source was edited. Monitor mode via `nethunter-wifi.sh conmode monitor 1`.

## Outcome

wifite ran the full normal handshake flow: found the target, discovered 2 clients,
issued 4 rounds of deauth (broadcast + both clients). At 987.9s uptime (~16 min):

    [  987.908244] cnss: fatal: SMMU fault happened with IOVA 0xa9770000
    [  988.436521] mhi: [E][mhi_process_sfr] NOC_error_patched.c:437:0x8 NOC ERROR DETECTED.

No handshake captured (`/tmp/ns/` empty) — the radio died mid-attack.

## Counters

| metric | value |
|---|---|
| submits | 253 |
| completions | 235 |
| backpressure drops | 215 (85%) |
| strict success (`stored == desc_id`) | **0 / 235** |
| **low16 success** (`(stored & 0xFFFF) == (desc_id & 0xFFFF)`) | **235 / 235** |
| slot derived from low16 wrong | **0** |
| inflight first / max / last | 1 / 30 / 30 |
| submit rate | 7.8 /s over 32.5 s |

## The bug, stated precisely

`hdd_mon_inject_tx_complete()` compares the echoed `desc_id` against the stored
one with a **strict 32-bit equality test**:

    if (g_inj.slots[idx].in_use && stored_desc == desc_id) { ... freed = true; }

But firmware echoes only the **low 16 bits** of `desc_id`. The high half carries
the seq (`(seq & 0x7FFF) << 16`), which firmware discards. So the comparison holds
**0 times in 235 completions** — proven again here, and 0/1501 in the earlier
mdk4-flood run. `freed` is never set, `inflight` is never decremented by a
completion, and the **time-based reaper becomes the only reclaim path**.

`inflight` is therefore a **one-way ratchet**: it only ever climbs (reaper
decrements are slower than any burst). The first burst past
`HDD_MON_INJECT_PRESSURE (16)` switches the reaper from `AGE_MS (1500 ms)` to
`AGE_FAST_MS (150 ms)` — and stays there for the rest of the session, because
inflight never comes back down.

Measured submit→completion latency (earlier run): p50 2.5 ms, p90 119.4 ms,
**p99 180.6 ms**, max 768.2 ms. So at `AGE_FAST_MS = 150 ms` the reaper unmaps
buffers the firmware is still DMA-reading ~4% of the time → SMMU translation
fault (FSR `TF R` = Translation Fault, **READ**) → NOC error → firmware hang.

**This is not a flood-only pathology.** Stock wifite's default deauth loop
sustained only 7.8 frames/s and still crashed, because the ratchet means the
*first* burst above 16 is permanent.

## The fix the evidence points to

Match completions on the **low 16 bits** (which uniquely identify the slot:
`0xF000 | idx`, idx 0..31), not the full 32-bit id — and keep at most **one
outstanding submit per slot**, so low16 matching is unambiguous (no ABA reuse).
Per-slot completion order is strictly FIFO (1496/1496 in submit order, proven in
the earlier run), so the oldest outstanding submit on a slot is the one that
completed. With correct decrementing, inflight stays ~1-3, `AGE_MS` (1500 ms)
applies, and the reaper reverts to being what its comment already claims: a
lost-completion safety net rather than the primary reclaim path.

---

## DEFINITIVE ROOT CAUSE (driver source, not inference)

The WMI mgmt-TX API struct is **16 bits wide** for desc_id:

    vendor/qcom/opensource/wlan/qca-wifi-host-cmn/wmi/inc/wmi_unified_param.h:1921
    struct wmi_mgmt_params {
        void    *tx_frame;
        uint16_t frm_len;
        uint8_t  vdev_id;
        uint8_t  tx_type;
        uint16_t chanfreq;
        uint16_t desc_id;      /* <-- 16 BITS */
        ...
    };

The patch does:

    mgmt.desc_id = (uint32_t)slot_id;      /* patch line 1349 */

`slot_id` is the full 32-bit id from `hdd_mon_inject_slot_put()`, e.g. `0xf6f015`.
Assigning it to the `uint16_t` field **truncates it in place** to `0xf015`. The
submit log then prints `mgmt.desc_id` *after* that assignment, which is exactly
why it reads `desc 0xf015` while the slot's stored `desc_id` is `0xf6f015`.

So the seq in bits[30:16] **never reaches firmware, and cannot** — the WMI field
has nowhere to put it. Firmware echoes back the only 16 bits it ever received.

### Consequence: the strict match is unsatisfiable, not merely buggy

    if (g_inj.slots[idx].in_use && stored_desc == desc_id)   /* 0xf6f015 == 0xf015 */

is false for **every frame, always**. This is not a race, a timing window, or a
firmware quirk that might differ per build — it is a type-width mismatch that
makes the completion path dead code on every device. Measured: 0/235 and 0/1501.
The patch comment claiming "firmware echoes the full desc_id (the normal case on
peach-v2)" is factually wrong; the high bits are never even transmitted.

### What this means for the fix

- The seq-in-high-bits disambiguation scheme is **dead on arrival**. Any design
  that relies on a 32-bit token or on the high half round-tripping is invalid.
- The ABA hazard the strict match was (wrongly) defending is **real**: slot reuse
  before completion does happen at `AGE_FAST_MS = 150 ms` (p99 latency 180 ms).
  All 32 slots took 7-8 submits each in 32 s, and all 235 completions arrived at a
  slot already holding a *newer* submission.
- The fix must fit in **16 bits**, and that is ample: the stock mgmt pool is
  `MGMT_DESC_POOL_MAX = 64` (configs/config_to_feature.h:2573), i.e. ids 0..63, so
  the range 64..65535 is entirely free for a rolling token.
- A token must be unique **among currently-outstanding submissions** (<= 30 at
  BACKPRESSURE), so a monotonically increasing 16-bit token that skips any value
  already outstanding makes ABA structurally impossible.

---

## Fix implemented (2026-09-25) — two independent SMMU triggers

The 16-bit token layout closes the ABA hole. Adversarial review of that fix
(`wf_4ca63010-ae4`, Refute phase, four lenses) then surfaced **two more ways
this code can unmap a buffer firmware is still DMA-reading**, both independent
of the token. Both are fixed in the same patch.

### Fix 1 — token entirely inside the 16-bit lane (the ABA hole)

`struct wmi_mgmt_params.desc_id` is `uint16_t`, so the varying part of the id
must live in bits[15:0]. New layout:

    bits[4:0]   slot          (32 slots)
    bits[11:5]  generation    (128 values, per-slot, bumped on every reuse)
    bits[15:12] 0xF           (HDD_MON_INJECT_DESC_BASE)

`hdd_mon_inject_slot_put` bumps the slot's generation each time it hands the
slot out, so a completion for a previous occupant carries a token the slot no
longer holds and the strict compare (`stored_desc == desc_id`) rejects it —
frees nothing. The compare is *kept*, and is now satisfiable (measured 235/235
low16 echo) and safe.

Verified against the **extracted kernel functions** (not a re-implementation):
`gcc -Wall -Wextra` clean; 4096 tokens, 4096 distinct, 0 `is_ours`/slot
errors; a stale completion after 200 slot reuses frees nothing; the reaper
reclaims a lost completion only past `AGE_MS`. Wrap margin: 4096 submits =
20.5 s even at a 200 fps flood vs a measured 768 ms worst-case latency (27x);
per-slot completion order is strictly FIFO (1496/1496), which closes it further.

### Fix 2 — the pressure branch shortened the safety window (introduced by me)

`AGE_FAST_MS` (3000) was **less than** `AGE_MS` (5000), so crossing
`PRESSURE` made the reaper reclaim *sooner* — exactly backwards. A deep
pipeline is the case where completions are slowest, so pressure is precisely
when the window must not shrink. Now there is a single load-independent
`AGE_MS = 5000` (~6.5x the measured 768 ms max); `PRESSURE` only makes the
reaper *check* more often, never lowers the bar for reclaiming.

Modelled against the measured latency CDF (p50 2.5 / p90 119 / p99 180.6 /
max 768 ms): at the old 150 ms the reaper unmapped a still-pending buffer
303 times per 30k submits at stock wifite's 7.8 fps, 1248 times under a 50 fps
flood; at 5000 ms, zero in every scenario.

### Fix 3 — teardown unmapped buffers before quiescing firmware

`hdd_mon_inject_helper_teardown` called `hdd_mon_inject_slots_flush()`
(unmap + free) **before** `WMI_VDEV_STOP` / `WMI_VDEV_DELETE`. While the ghost
vdev is still STARTED in firmware, an outstanding buffer can still be one
firmware is DMA-reading, so flushing first is the same SMMU fault class as
Fix 2. Firmware is the only thing that can prove it is done with a buffer, so
the vdev teardown is now that proof: the flush moved after `VDEV_DELETE`.
The two early-exit paths (no ghost vdev was ever created; WMI unreachable so
firmware state cannot be quiesced) still flush, since in those cases there is
no firmware hold to respect.

### Residual risk (documented, not proven away)

The reaper is still a timer, and a host cannot distinguish "completion lost"
from "completion very late", so no finite age is *provably* safe — 5000 ms is
a heuristic with 6.5x margin over the worst measured hold, not a proof. A
fully safe design would move aged nbufs to a bounded quarantine and defer the
unmap to vdev stop/SSR/unload; that is a larger change than this fix, and the
age margin plus FIFO ordering make the current risk remote.
