# SM8750 (OnePlus 13) 802.11 Frame Injection — Deep Research

> Branch: `fix/hidden-vdev-state`  
> Date: 2026-08-14  
> Author: ratman4080  

---

## 1. Executive Summary

The OnePlus 13 uses Qualcomm's Kiwi (SM8750) SoC with the qcacld-3.0 WLAN driver.
The firmware **rejects management TX on MONITOR-type vdevs** — it falls to a
beacon-only path and discards the frame. The only working injection path
requires creating a hidden STA vdev that the firmware accepts as a TX endpoint.

Our current patch (`qcacld-monitor-injection.patch`, 983 lines) implements this
hidden-helper-STA approach. It **compiles and loads**, but has several critical
gaps compared to the reference implementation (Loukious `wma_frame_inject.c`,
2118 lines) and fundamental issues with the WMI TX path on SM8750 that need
resolution.

**Three WMI injection paths exist in the SM8750 firmware API:**

| Path | WMI Command ID | Frame Types | DMA Map | Completion Event |
|---|---|---|---|---|
| Management TX | `WMI_MGMT_TX_SEND_CMDID` | 802.11 management only | ✅ `qdf_nbuf_map_single` | `WMI_MGMT_TX_COMPLETION_EVENTID` |
| Off-channel Data TX | `WMI_OFFCHAN_DATA_TX_SEND_CMDID` | RAW data frames | ✅ `qdf_nbuf_map_single` | `WMI_OFFCHAN_DATA_TX_COMPLETION_EVENTID` |
| PDEV Frame Inject | `WMI_PDEV_FRAME_INJECT_CMDID` | Periodic: QoS NULL, CTS-to-self, or host buffer | N/A (template) | N/A (periodic) |
| QoS NULL TX | `WMI_QOS_NULL_FRAME_TX_SEND_CMDID` | QoS Null frames | ✅ DMA paddr | `WMI_QOS_NULL_FRAME_TX_COMPLETION_EVENTID` |

**Key finding**: `WMI_OFFCHAN_DATA_TX_SEND_CMDID` can transmit **raw data frames**
on an off-channel, which opens the door to data-frame injection beyond
management-only. The firmware comment says: *"it will be always be RAW frame,
as it will be tx'ed on non-pause tid"*.

---

## 2. Architecture: Why Hidden Helper STA Vdev

### 2.1 Firmware rejects MONITOR vdev mgmt TX

The firmware function `_wlan_send_mgmt_to_host` (or its modern equivalent in
Kiwi firmware) checks the vdev type. MONITOR vdevs fall into a beacon-TX-only
code path. Management frames submitted on a MONITOR vdev are silently discarded.

### 2.2 pkt_capture is CAPTURE ONLY — dead end

I read the entire `wlan_pkt_capture_data_txrx.c` from the SM8750 OSS repo.
- `pkt_capture_rx_data_cb` processes RX frames
- `pkt_capture_tx_data_cb` processes TX completion callbacks for frames the
  firmware **already transmitted** through normal paths

There is **zero injection capability**. The vendor command
`QCA_NL80211_VENDOR_SUBCMD_SET_MONITOR_MODE` activates capture mode only.

The SM8750 defconfig (`sun_gki_kiwi-v2_defconfig`) enables:
- `CONFIG_FEATURE_MONITOR_MODE_SUPPORT=y`
- `CONFIG_WLAN_FEATURE_PKT_CAPTURE=y`
- `CONFIG_WLAN_FEATURE_PKT_CAPTURE_V2=y`
- `CONFIG_QCA_MONITOR_PKT_SUPPORT=y`
- `CONFIG_WLAN_DP_LOCAL_PKT_CAPTURE=y`

All of these are **RX/capture** features. None provide TX injection.

### 2.3 P2P path is management-only with peer lookup

The P2P off-channel TX path (`wlan_p2p_off_chan_tx.c`) explicitly rejects
non-management frames (`if (type != P2P_FRAME_MGMT) return E_FAILURE`). It
also requires an `wlan_objmgr_get_peer` match — spoofed frames with
non-matching MAC addresses are asynchronously dropped. The P2P path is
unsuitable for general injection.

### 2.4 Hidden STA vdev is the only viable path

A hidden STA vdev with a self-peer on the monitor channel is the minimum
viable injection endpoint because:
1. Firmware accepts management TX on STA vdevs
2. Self-peer satisfies firmware peer-lookup for the MAC address
3. No beacon-TX-offload (unlike AP vdevs, which crash the firmware)
4. VDEV_UP is skipped — firmware asserts for STA without a BSS peer

---

## 3. Current Patch Analysis

### 3.1 Architecture overview

```
wlan_hdd_main.c (HDD layer)
  ├─ hdd_mon_hard_start_xmit()     — ndo_start_xmit for monitor interface
  │   ├─ hdd_mon_inject_strip_radiotap()  — strip radiotap header
  │   ├─ ieee80211_is_mgmt() check        — management-only filter
  │   └─ wma_mon_inject_tx(skb, chanfreq) — pass to WMA
  ├─ hdd_mon_inject_set_channel()  — create/reconfigure helper vdev
  └─ hdd_mon_inject_stop()         — teardown helper vdev

wma_frame_inject.c (WMA layer — new file)
  ├─ wma_mon_inject_init/deinit()  — global state lifecycle
  ├─ wma_mon_inject_setup()        — VDEV_CREATE → VDEV_START → PEER_CREATE
  ├─ wma_mon_inject_teardown()     — PEER_DELETE → VDEV_STOP → VDEV_DELETE
  ├─ wma_mon_inject_tx()           — allocate WMI buf, copy frame, send
  ├─ wma_mon_inject_tx_complete()  — intercept in wma_mgmt.c completion path
  └─ wma_mon_inject_reaper_cb()    — periodic stale-nbuf cleanup

wma_mgmt.c (hooked completion)
  └─ wma_process_mgmt_tx_completion() — calls wma_mon_inject_tx_complete() first
```

### 3.2 What works

1. **Compilation and module load** — the patch compiles against SM8750 qcacld-3.0
   and the kernel module loads successfully.

2. **Helper vdev creation** — `wma_mon_inject_setup()` sends WMI
   VDEV_CREATE(STA) → msleep(150) → VDEV_START → msleep(150) → PEER_CREATE →
   msleep(100). The firmware accepts these commands for a vdev slot with no
   host `wlan_objmgr_vdev` object.

3. **Descriptor isolation** — injection desc_ids (0x8001–0x9000) don't collide
   with the normal mgmt_txrx pool. The completion hook in `wma_mgmt.c` checks
   `wma_mon_inject_is_desc_id()` first and consumes injection completions.

4. **Lifecycle locking** — `lifecycle_lock` (qdf_mutex) serializes setup/teardown,
   `state_lock` (qdf_spinlock) protects inflight state. No races between TX
   and teardown.

5. **Reaper** — 3-second periodic cleanup for stale nbufs (2-second timeout).

6. **VDEV slot selection** — scans `wma->interfaces[i]` for empty slots,
   avoids the monitor vdev ID, respects `max_bssid` limits.

### 3.3 Critical issues

#### CRITICAL 1: DMA-mapped nbuf never unmapped

The WMI TLV layer (`send_mgmt_cmd_tlv` in `wmi_unified_tlv.c`) does:
```c
qdf_nbuf_map_single(qdf_ctx, param->tx_frame, QDF_DMA_TO_DEVICE);
dma_addr = qdf_nbuf_get_frag_paddr(param->tx_frame, 0);
cmd->paddr_lo = (uint32_t)(dma_addr & 0xffffffff);
```

Our `wma_mon_inject_tx()` passes the `wmi_buf` nbuf as `mgmt.tx_frame`.
The WMI layer DMA-maps it and passes the physical address to firmware.
Firmware DMA-reads the frame data from that address.

**On completion**, `wma_process_mgmt_tx_completion()` calls:
```c
buf = mgmt_txrx_get_nbuf(pdev, desc_id);  // returns NULL — not in pool
if (buf)
    wma_mgmt_unmap_buf(wma_handle, buf);   // skipped — buf is NULL
```

Our hook `wma_mon_inject_tx_complete()` correctly frees the nbuf, but it
**never calls `qdf_nbuf_unmap_single`** before freeing. The DMA mapping leaks
on every frame. On SM8750 (SNOC/LL path), this is a **SMMU mapping leak** —
eventually the IOMMU pool exhausts and the device crashes.

**Loukious does this correctly** via `wma_injection_unmap_tx_buf()` which calls
`qdf_nbuf_unmap_single(qdf_ctx, buf, QDF_DMA_TO_DEVICE)` on the LL path
and is a no-op on the HL path.

**Fix**: Add DMA unmap before nbuf free in both `wma_mon_inject_tx_complete()`
and `wma_mon_inject_reaper_cb()` and `wma_mon_inject_flush_inflight()`.

#### CRITICAL 2: WMI TX copies frame inline AND DMA-maps the nbuf

The `send_mgmt_cmd_tlv()` function does **two things** with the frame:
1. Copies the first `bufp_len` bytes inline into the WMI command TLV
   (`WMI_HOST_IF_MSG_COPY_CHAR_ARRAY(bufp, param->pdata, bufp_len)`)
2. DMA-maps `param->tx_frame` and passes `paddr_lo/paddr_hi` to firmware

Firmware uses the inline copy for small frames and DMA for the rest. But our
patch allocates a separate `wmi_buf` nbuf, copies the frame into it, then
passes that as both `tx_frame` (for DMA) and `pdata` (for inline copy).

The problem: we copy `skb->data` → `wmi_buf`, then pass `frame_data` (pointing
into `wmi_buf`) as `pdata` AND `wmi_buf` as `tx_frame`. The inline copy gets
the right data. The DMA path maps the same `wmi_buf`. This actually works
correctly for data integrity — firmware gets the frame either way.

However, the **HDD layer also frees the original `skb`** after `wma_mon_inject_tx()`
returns 0 (line 183: `qdf_nbuf_free(skb)`). This is correct — the HDD comment
says "The caller retains ownership of @skb on both success and failure" but the
HDD code always frees it. Our WMA copy is independent.

This is actually fine. The real issue is CRITICAL 1 (DMA unmap).

#### CRITICAL 3: Firmware may not send completions for helper vdev

The helper STA vdev has **no host wlan_objmgr_vdev object**. When the firmware
sends the WMI MGMT_TX_COMPLETION event, the WMI event handler calls
`wma_process_mgmt_tx_completion()` which calls `mgmt_txrx_get_nbuf(pdev, desc_id)`.

For our injection desc_ids, this returns NULL (not in the mgmt_txrx pool).
Then `mgmt_txrx_tx_completion_handler()` is called — this may warn or crash
if the desc_id is outside its pool range.

Our hook intercepts **before** `wma_process_mgmt_tx_completion()` is called
(in `wma_mgmt.c`, right at the top of the handler). So we consume the
completion and return 0 before the pool lookup happens. This is correct.

But: **does firmware actually send completions for the helper vdev?**

If the helper vdev is in STARTED state with a self-peer, the firmware's
WAL-TX path should transmit the frame and send a completion. However:
- If firmware drops the frame (e.g., no valid peer for the destination MAC),
  it may send a DISCARD completion or no completion at all.
- Our reaper handles the no-completion case (2s timeout).

**Verdict**: The completion path is architecturally correct but untested.
If firmware doesn't send completions, the reaper frees stale nbufs every 3s.
The DMA unmap leak (CRITICAL 1) still applies.

#### HIGH 1: No backpressure on inflight count

Our patch has 64 inflight slots but no atomic counter or high-water-mark
check before submission. If firmware silently drops frames (no completions),
all 64 slots fill up, then every new TX returns -EBUSY. The caller (HDD)
drops the frame silently.

**Loukious** uses `qdf_atomic_t inflight_count` with
`WMA_INJECTION_INFLIGHT_HIGH = 200` and returns `QDF_STATUS_E_RESOURCES`
when exceeded. The reaper gradually frees stale entries.

**Fix**: Add `qdf_atomic_t inflight_count`, check before TX submission.

#### HIGH 2: next_desc_id starts at WMA_MON_INJECT_DESC_ID_BASE (0x8001)

The first injected frame uses desc_id 0x8001. If the normal mgmt_txrx pool
also allocates IDs in this range, there's a collision. In practice, the
mgmt_txrx pool uses its own ID space, but this should be verified.

**Loukious** uses `WMA_INJECTION_DESC_ID_BASE = 0x2000` with mask 0x0FFF.
Our 0x8001 is safer (further from normal pool), but the initial value
should skip ID 0 to avoid ambiguity.

#### HIGH 3: Probe-request SA not normalized

The firmware's management TX handler may discard broadcast probe requests
whose SA doesn't match the transmitting vdev's MAC. Our patch doesn't
normalize the SA field (offset 10 in the 802.11 header) to match the
helper vdev MAC.

**Loukious** explicitly overrides SA for broadcast probe requests on
monitor vdevs:
```c
if (monitor_vdev && is_probe_req && is_bcast_da && req->frame_len >= 24)
    qdf_mem_copy(frame_data + 10, vdev_mac, QDF_MAC_ADDR_SIZE);
```

**Fix**: Add SA normalization in `wma_mon_inject_tx()` for probe requests.

#### MEDIUM 1: chanfreq=0 for probe requests

The P2P/host management TX path uses `chanfreq=0` for probe requests
(meaning "use current channel"). Our patch always passes the explicit
channel frequency. This should work, but some firmware builds may
behave differently with explicit vs. zero chanfreq for probes.

**Loukious** uses:
```c
mgmt_params.chanfreq = is_probe_req ? 0 : tx_chanfreq;
if (monitor_vdev && is_probe_req && tx_chanfreq)
    mgmt_params.chanfreq = tx_chanfreq;
```

#### MEDIUM 2: Helper MAC may equal monitor MAC

If the monitor MAC already has the locally-administered bit set AND the
last bit is 1, then `dst[0] |= 0x02; dst[5] ^= 0x01` produces a MAC
identical to the monitor MAC. This would cause the firmware to reject
PEER_CREATE (duplicate MAC on same pdev).

**Fix**: If derived MAC equals monitor MAC, flip a different bit or
use a completely random locally-administered address.

#### MEDIUM 3: No WMI service check before using mgmt TX WMI path

The `wma_mgmt_unified_cmd_send()` wrapper checks `wmi_service_mgmt_tx_wmi`
before choosing between the WMI path and the legacy CDP path. Our patch
calls `wmi_mgmt_unified_cmd_send()` directly (the low-level WMI function),
bypassing this check.

On SM8750 (SNOC transport), `wmi_service_mgmt_tx_wmi` should be enabled.
But if it's not, our WMI command will fail silently or be ignored by
firmware.

**Fix**: Check `wmi_service_enabled(wma->wmi_handle, wmi_service_mgmt_tx_wmi)`
before sending. Fall back to `cdp_mgmt_send_ext()` if unavailable (like
Loukious does for non-monitor vdevs).

---

## 4. Gap Analysis: Our Patch vs. Loukious Reference

| Feature | Our Patch | Loukious | Impact |
|---|---|---|---|
| DMA unmap on completion | ❌ Missing | ✅ `wma_injection_unmap_tx_buf()` | **CRITICAL** — SMMU leak |
| DMA unmap on reaper cleanup | ❌ Missing | ✅ | **CRITICAL** — SMMU leak |
| Backpressure (inflight high-water) | ❌ Missing | ✅ 200 limit | HIGH — OOM under load |
| Inflight atomic counter | ❌ Missing | ✅ `qdf_atomic_t` | HIGH — race potential |
| Debug cache (256 entries) | ❌ 64-slot array | ✅ hash by desc_id | LOW — ours is simpler but works |
| Probe-req SA normalization | ❌ Missing | ✅ Override SA to vdev MAC | HIGH — firmware drops probes |
| chanfreq=0 for probe reqs | ❌ Always explicit | ✅ Conditional | MEDIUM — firmware compatibility |
| WMI service check | ❌ Missing | ✅ `wmi_service_mgmt_tx_wmi` | MEDIUM — robustness |
| CDP legacy fallback | ❌ Missing | ✅ `cdp_mgmt_send_ext` | LOW — SM8750 uses WMI |
| Logging (first-N per status) | ❌ Basic | ✅ Rate-limited detailed logs | LOW — debugging |
| Queue/throttle infrastructure | ❌ Direct TX | ✅ Queue + work thread | LOW — adds latency control |
| Frame type check | HDD: mgmt only | WMA: passes all to WMI | LOW — firmware enforces |
| VDEV type | STA (correct) | STA (was AP, fixed to STA) | ✅ Same |
| Skip VDEV_UP | ✅ | ✅ | ✅ Same |
| Object-manager vdev reservation | ❌ Not reserved | ❌ Not reserved | ⚠️ Both have this gap |
| Stats (processed/dropped/fw_errors) | ❌ Missing | ✅ Detailed | LOW — observability |
| Session state reset | ❌ Missing | ✅ On new vdev create | LOW — counter hygiene |

---

## 5. Paths to Data/Control Frame Injection

### 5.1 WMI_OFFCHAN_DATA_TX_SEND_CMDID — RAW data frame injection

The firmware API defines `wmi_offchan_data_tx_send_cmd_fixed_param`:
```c
typedef struct {
    A_UINT32 vdev_id;
    A_UINT32 desc_id;   /* echoed in tx_compl_event */
    A_UINT32 chanfreq;  /* MHz units */
    A_UINT32 paddr_lo;
    A_UINT32 paddr_hi;
    A_UINT32 frame_len;
    A_UINT32 buf_len;   // "it will be always be RAW frame, as it will be tx'ed on non-pause tid"
    A_UINT32 tx_params_valid;
} wmi_offchan_data_tx_send_cmd_fixed_param;
```

Completion: `WMI_OFFCHAN_DATA_TX_COMPLETION_EVENTID` with `desc_id` + `status`.

The host WMI layer has this wired into the ops table:
```c
.send_offchan_data_tx_cmd = send_offchan_data_tx_cmd_tlv,
```

And the API function:
```c
QDF_STATUS wmi_offchan_data_tx_cmd_send(wmi_unified_t wmi_handle,
    struct wmi_offchan_data_tx_params *param);
```

**This is the path to data-frame injection.** It accepts raw 802.11 data frames
and transmits them on the specified channel through the helper vdev. The firmware
treats them as RAW frames on a non-pause TID, bypassing the management-frame-only
restriction.

**Caveat**: We need to verify:
1. Does the SM8750 firmware actually implement this handler? (The host-side code
   exists, but firmware could return an error or ignore the command.)
2. Does it work on our helper STA vdev, or does it require a specific vdev type?
3. Are there firmware service bits that gate this feature?

### 5.2 WMI_QOS_NULL_FRAME_TX_SEND_CMDID — control-like injection

```c
typedef struct {
    A_UINT32 vdev_id;
    A_UINT32 desc_id;
    A_UINT32 paddr_lo;
    A_UINT32 paddr_hi;
    A_UINT32 frame_len;
    A_UINT32 buf_len;
} wmi_qos_null_frame_tx_send_cmd_fixed_param;
```

This sends QoS Null frames. Not useful for general injection, but confirms the
firmware has multiple TX paths beyond management frames.

### 5.3 WMI_PDEV_FRAME_INJECT_CMDID — periodic frame injection

```c
enum wmi_frame_inject_type {
    WMI_FRAME_INJECT_TYPE_QOS_NULL,
    WMI_FRAME_INJECT_TYPE_CTS_TO_SELF,
    WMI_FRAME_INJECT_TYPE_HOST_BUFFER,  // <-- arbitrary buffer
    WMI_FRAME_INJECT_TYPE_MAX,
};
```

`WMI_FRAME_INJECT_TYPE_HOST_BUFFER` allows injecting an arbitrary frame template
at a configurable period. This is designed for traffic shaping / keepalive, not
single-shot injection. But it confirms firmware can TX arbitrary 802.11 frames.

### 5.4 Assessment: Data frame injection is feasible

The `WMI_OFFCHAN_DATA_TX_SEND_CMDID` path is the most promising for extending
injection beyond management frames. The host-side WMI infrastructure exists and
is wired into the ops table. The firmware API defines both the command and
completion event.

**Implementation plan for data-frame injection:**
1. In `wma_mon_inject_tx()`, detect frame type from the 802.11 header
2. For management frames: use existing `wmi_mgmt_unified_cmd_send()` path
3. For data frames: use `wmi_offchan_data_tx_cmd_send()` path
4. Remove the `ieee80211_is_mgmt()` filter in HDD
5. Handle `WMI_OFFCHAN_DATA_TX_COMPLETION_EVENTID` completions in the
   injection completion hook
6. Test with a simple data frame (e.g., a raw 802.11 data frame with
   broadcast DA)

### 5.5 Control frames (ACK, CTS, RTS) — not directly possible

Control frames are < 24 bytes and are generated by firmware/hardware, not
the host. The WMI paths all require frames ≥ 24 bytes. The
`WMI_FRAME_INJECT_TYPE_CTS_TO_SELF` provides periodic CTS-to-self but not
single-shot. There is no general path for injecting arbitrary control frames.

---

## 6. What Needs to Happen (Priority Order)

### Phase 1: Fix current management-frame injection (CRITICAL)

1. **Add DMA unmap** — `qdf_nbuf_unmap_single()` before every `qdf_nbuf_free()`
   in `wma_mon_inject_tx_complete()`, `wma_mon_inject_reaper_cb()`, and
   `wma_mon_inject_flush_inflight()`. Use `cds_get_context(QDF_MODULE_ID_QDF_DEVICE)`
   for the `qdf_ctx` handle.

2. **Add inflight backpressure** — `qdf_atomic_t inflight_count` in
   `wma_mon_inject_ctx`. Increment on WMI submission success, decrement in
   completion/reaper/flush. Check against `WMA_MON_INJECT_MAX_INFLIGHT` before
   submitting.

3. **Add probe-req SA normalization** — In `wma_mon_inject_tx()`, for frames
   with FC type=0x00 subtype=0x40 (probe request) and broadcast DA, copy the
   helper vdev MAC to the SA field (offset 10).

4. **Add WMI service check** — Before calling `wmi_mgmt_unified_cmd_send()`,
   check `wmi_service_enabled(wma->wmi_handle, wmi_service_mgmt_tx_wmi)`.

5. **Fix next_desc_id initialization** — Start at `WMA_MON_INJECT_DESC_ID_BASE + 1`
   to avoid desc_id 0x8001 on first frame if that causes issues.

### Phase 2: Test and validate

1. Flash kernel with fixes
2. Enable monitor mode: `iw phy0 interface add mon0 type monitor && ip link set mon0 up`
3. Set channel: `iw dev mon0 set channel 1`
4. Inject a deauth frame: `aireplay-ng -0 1 -a <AP_MAC> mon0`
5. Verify with a second device in monitor mode that the frame appears OTA
6. Check dmesg for injection completion status (OK vs DISCARDED)

### Phase 3: Extend to data-frame injection (HIGH)

1. Wire up `WMI_OFFCHAN_DATA_TX_SEND_CMDID` in `wma_mon_inject_tx()`
2. Remove `ieee80211_is_mgmt()` filter in HDD
3. Register for `WMI_OFFCHAN_DATA_TX_COMPLETION_EVENTID` completions
4. Test with raw 802.11 data frames

---

## 7. Open Questions

1. **Does SM8750 firmware actually send WMI MGMT_TX_COMPLETION events for the
   helper vdev?** If not, the reaper handles cleanup, but we lose TX status
   visibility. Need empirical testing.

2. **Does `WMI_OFFCHAN_DATA_TX_SEND_CMDID` work on a STA vdev in STARTED
   (not UP) state?** The management path works without VDEV_UP; the data path
   may have different requirements.

3. **What is `mgmt_tx_dl_frm_len`?** This variable controls how many bytes
   are copied inline vs. DMA'd. If it's 0 or very small, all frame data goes
   via DMA and the inline copy is empty. If it's ≥ frame length, no DMA is
   needed and we can skip the DMA map/unmap entirely. Need to check the
   SM8750 firmware configuration.

4. **Are there firmware service bits gating `WMI_OFFCHAN_DATA_TX_SEND_CMDID`?**
   Need to check if `wmi_service_offchan_data_tx` or similar exists.

5. **Can we use `wmi_unified_vdev_create_send()` directly (like our patch does)
   or should we go through the vdev manager?** The Loukious patch also calls
   the WMI function directly. Both work because the helper vdev intentionally
   has no host objmgr representation.

---

## 8. Reference Implementations

| Implementation | SoC | qcacld version | Notes |
|---|---|---|---|
| [Loukious/sm8150](https://github.com/Loukious/android_kernel_xiaomi_sm8150/blob/65c6a05ecd9b25ebf0742d39987c6a8a042227f1/drivers/staging/qcacld-3.0/core/wma/src/wma_frame_inject.c) | SM8150 | qcacld-3.0 (older) | Gold standard. 2118 lines. Queue + work thread + DMA unmap + backpressure + debug cache |
| [kimocoder/qualcomm_android_monitor_mode](https://github.com/kimocoder/qualcomm_android_monitor_mode) | Various | Various | Monitor mode enablement only, no injection |
| Our patch (`fix/hidden-vdev-state`) | SM8750 | qcacld-3.0 (Kiwi) | 983 lines. Direct TX, no DMA unmap, no backpressure |

---

## 9. SM8750 Defconfig Summary

From `sun_gki_kiwi-v2_defconfig` + `kiwi_defconfig` + `default_defconfig`:

```
CONFIG_FEATURE_MONITOR_MODE_SUPPORT=y
CONFIG_WLAN_FEATURE_PKT_CAPTURE=y
CONFIG_WLAN_FEATURE_PKT_CAPTURE_V2=y
CONFIG_QCA_MONITOR_PKT_SUPPORT=y
CONFIG_WLAN_DP_LOCAL_PKT_CAPTURE=y
CONFIG_WIFI_MONITOR_SUPPORT=y
CONFIG_CNSS_KIWI=y
CONFIG_INCLUDE_HAL_KIWI=y
CONFIG_WLAN_FEATURE_11BE=y
CONFIG_WLAN_FEATURE_11BE_MLO=y
CONFIG_CFG_MAX_STA_VDEVS=4
CONFIG_CHIP_VERSION=1
```

`WLAN_FEATURE_PKT_CAPTURE` is enabled but **capture-only**. No
`FEATURE_FRAME_INJECTION_SUPPORT` or similar exists in the defconfig —
our patch adds this capability from scratch.

---

*End of research document.*
