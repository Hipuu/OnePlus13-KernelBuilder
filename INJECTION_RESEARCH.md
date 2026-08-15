# OnePlus 13 `qcacld-3.0` management-frame injection: engineering handoff

> Research-only handoff. No driver, build, workflow, config, or manifest change is
> part of this document.
>
> Researched: 2026-08-15
>
> Builder commit: `b5462a2ad71943277c54ff8321f68b539af82abc`
>
> OnePlus source commit: `d50b305f7da9e14715a25120a4ac7b1a4b8b97c3`

## 1. Decision summary

The current hidden-STA implementation is **not fixed and should not be used for
stress testing**. It contains independent lifecycle, descriptor, and DMA
ownership bugs that can explain the missing over-the-air frames and can also
cause a firmware/SMMU failure.

The next agent should not begin by adding another delay or by extending the
stale-buffer reaper. The recommended order is:

1. Replace the current TX experiment with a one-frame proof using the **existing
   real monitor vdev, its real self-peer, and the stock management-TX pipeline**.
   This path already owns vdev IDs, WMI response timers, DP state, descriptors,
   DMA mappings, completions, and recovery.
2. Record firmware completion status and independently capture the frame on a
   second radio. The open-source host code does **not** prove the proprietary
   OnePlus firmware rejects management TX on a monitor vdev.
3. Only if the monitor-vdev proof returns a reproducible firmware rejection,
   prototype a **real host-managed P2P-device helper vdev**. It has a normal
   object-manager ID and a normal self-peer. Test its create/start/stop/delete
   lifecycle before sending a frame.
4. Keep a firmware-only or “ghost” STA helper as the last resort. It can only be
   made defensible with real response interception, collision-free ID
   reservation, strict TX quiescence, and quarantining of any DMA buffer whose
   completion is missing. A timeout is not permission to unmap and free it.

Scope the first working version to **on-channel, unprotected 802.11 management
frames on 2.4/5 GHz at 20 MHz**. Data/control-frame injection, 6 GHz, wide
channels, arbitrary rate control, protected frames, and bit-exact sequence/FCS
preservation are separate projects.

## 2. Exact target and evidence level

The relevant build is `configs/OP13-6.6.118.json`:

- `ddk: true`
- `ddk_target: sun_perf`
- `ddk_chipset: peach-v2`
- `ddk_injection: true`
- output module: `qca_cld3_peach_v2.ko`

`manifests/a16/oneplus_13_6.6.118_w.xml` pins the OnePlus modules repository to
`d50b305f7da9e14715a25120a4ac7b1a4b8b97c3`. A remote check on 2026-08-15
showed that this was also the head of OnePlus's
`oneplus/sm8750_b_16.0.0_oneplus_13` branch. All source conclusions below are
from that exact revision, not from an older qcacld tree.

Evidence labels used here:

- **Confirmed in source**: directly follows from the pinned host-driver code.
- **Confirmed in current patch**: directly follows from the patch in this repo.
- **Observed on device**: supplied runtime result from the OnePlus 13.
- **Inference**: likely, but needs a completion status, firmware log, or OTA
  capture.
- **Unproven claim**: appears in a third-party patch/comment but cannot be
  established from the open-source host code.

The WLAN firmware is proprietary. A successful `wmi_unified_*_send()` return
only proves that the host accepted/queued a command. It does not prove the
firmware completed the state transition or transmitted a frame.

## 3. What is known from the device

Observed:

- `con_mode=4` loads and produces an operational radiotap monitor netdev.
- Earlier versions also exposed a `null` STA netdev. The current patch does not
  create that netdev; it creates only a firmware-side helper vdev.
- The earlier channel synchronization code timed out after
  `WMI_VDEV_START`, because it polled `bss_chan->ch_freq` for a vdev that had no
  host `wlan_objmgr_vdev`.
- The current patch replaced response waiting with fixed sleeps and can queue
  frames host-side, but no `INJECT_TEST` beacon was captured by an independent
  radio.
- Repeated sends eventually changed to `ENXIO`/“No such device or address” in
  one test. That is consistent with interface teardown or recovery, but it is
  not by itself proof of a firmware crash. Driver/CNSS logs are required.

The historical `bss_chan` timeout is explained by the stock response path:
`wma_vdev_start_resp_handler()` copies `des_chan` to `bss_chan` only after it
finds a real host vdev. A ghost vdev has no object to update. The correct signal
for such a vdev is the extracted VDEV_START response and its status, not polling
`bss_chan`.

## 4. Current patch architecture

Current file:
`.github/actions/build-kernel/files/ddk/qcacld-monitor-injection.patch`

The patch currently does this:

```text
monitor ndo_start_xmit
  -> linearize skb
  -> validate and strip radiotap length
  -> allow management frames only
  -> copy 802.11 frame into a new qdf_nbuf
  -> WMI_MGMT_TX_SEND on a firmware-only STA helper vdev
  -> custom descriptor table
  -> custom WMA completion interception or a 2-second timeout reaper
```

Helper lifecycle:

```text
WMI VDEV_CREATE(STA)
  -> sleep 150 ms
  -> WMI VDEV_START
  -> sleep 150 ms
  -> WMI PEER_CREATE(self)
  -> sleep 100 ms
  -> declare READY
```

Teardown flushes all TX buffers first, then sends PEER_DELETE, VDEV_STOP, and
VDEV_DELETE with 100 ms sleeps. It does not wait for firmware response events.

## 5. Confirmed defects in the current patch

### 5.1 Every allocation returns the same descriptor ID

This is a deterministic bug, not a theory. The constants are:

```c
#define WMA_MON_INJECT_DESC_ID_BASE 0x8001
#define WMA_MON_INJECT_DESC_ID_MASK 0x0fff
```

The allocator increments `next_desc_id`, then tests:

```c
if ((next_desc_id & ~MASK) != BASE)
        next_desc_id = BASE + 1;
```

For any value in the intended range, `value & ~0x0fff` is `0x8000`, which can
never equal the unaligned base `0x8001`. With the current initialization, every
call returns `0x8002` and resets back to `0x8002`. Concurrent frames therefore
share an ID; a completion can release the wrong buffer, and later completions
become ambiguous.

The stock management descriptor pool is IDs 0 through 63
(`MGMT_DESC_POOL_MAX` defaults to 64). A private range can be outside that pool,
but the pinned host API stores `desc_id` as `uint16_t`, and every private
completion must be consumed before stock code indexes the 64-entry array.

If a private allocator is retained, use an aligned range such as
`0x8000..0x8fff`, allocate under the same lock as the in-flight table, scan for
an unused ID, and never reset the sequence on helper recreation. More
importantly, do not reuse an ID whose old completion remains possible. If
firmware never completes frames, safe indefinite ID reuse is impossible without
a firmware reset/quiescence guarantee.

### 5.2 A timer frees memory that firmware may still DMA-read

The pinned `send_mgmt_cmd_tlv()`:

1. copies a prefix inline into the WMI message;
2. DMA-maps the complete `tx_frame` nbuf;
3. sends its physical address to firmware; and
4. unmaps it only on local command-send failure.

After a successful submission, ownership of that mapping lasts until TX
completion or another documented device-quiescence boundary. The current
2-second reaper unmaps and frees solely because time elapsed. Firmware can then
DMA from reused memory. This can produce corrupted TX, use-after-DMA, an SMMU
fault, or recovery.

The same bug exists in teardown: `wma_mon_inject_flush_inflight()` unmaps/frees
all buffers **before** PEER_DELETE/VDEV_STOP/VDEV_DELETE are sent. Unmapping is
not cancellation. Linux DMA rules require the device to have finished using a
streaming mapping before unmap/free.

Required rule: a successful WMI submission is `FW_OWNED`. Release it only on
the matching completion, or after a device reset/firmware-recovery boundary
that guarantees DMA has stopped. A timeout may log and quarantine the buffer;
it must not free it.

### 5.3 TX races teardown and can use a freed nbuf

`wma_mon_inject_tx()` publishes an in-flight slot, drops `state_lock`, rewrites
the copied frame, and calls WMI. It does not hold `lifecycle_lock` and has no
active-submitter barrier. Teardown can run between those operations, flush and
free the nbuf, zero the helper MAC, and then let TX continue with freed memory.

The same race can make teardown call DMA-unmap on a buffer that has not yet been
mapped. A very fast completion can also decrement `inflight_count` before the
TX function increments it. The atomic counter is therefore not synchronized
with slot ownership and can underflow or diverge.

Fixing this requires:

- blocking new submissions before teardown;
- an `active_submitters` count/ref with a waitable drain;
- all slot transitions and the in-flight count under one lock;
- copying vdev ID, MAC, channel, device handle, and generation while protected;
- teardown waiting for active submitters before inspecting FW-owned slots.

### 5.4 Setup error unwind does nothing

The state remains `DOWN` throughout create/start/peer setup. On a partial
failure, `fail_teardown` calls a function that immediately returns when state is
`DOWN`. A created or started firmware vdev can therefore leak.

Track command phases independently, for example:

```text
create_sent
start_sent / start_acked
peer_create_sent / peer_create_acked
```

Unwind based on those flags even if the final `READY` state was never reached.

### 5.5 Fixed sleeps are not acknowledgements

The pinned host stack has real VDEV_START, VDEV_STOP, and VDEV_DELETE response
events. Their normal target-interface handlers require a response timer and a
host vdev. A ghost helper has neither, so the normal handlers return early.

The older repository commit `4b004b9` was directionally better: it intercepted
exact helper responses before the normal timer/object lookup. That approach
should be restored if a ghost helper remains, but matching must require the
active helper vdev ID, lifecycle phase, and generation so normal events are
never stolen.

VDEV_CREATE has no corresponding create response in this host API. WMI command
ordering plus a successful START response is the first useful confirmation that
the created vdev exists.

### 5.6 Peer responses are service-dependent

The normal peer-create confirmation handler expects a matching
`WMA_PEER_CREATE_REQ`. A direct ghost-helper command creates no such request, so
the event is otherwise discarded. Firmware advertises peer-create confirmation
through `wmi_service_peer_create_conf`, which the host maps to
`WLAN_SOC_F_PEER_CREATE_RESP`.

The normal driver explicitly falls back to legacy “command accepted” behavior
when that service is absent. A helper must do the same honestly:

- if peer-create confirmation is advertised, intercept the exact helper
  vdev+MAC response and wait for its status;
- if absent, label peer creation unconfirmed and restrict the experiment;
- for deletion, wait for the exact peer-delete response only when
  `wmi_service_sync_delete_cmds` is advertised.

### 5.7 The ghost vdev ID is not reserved

The helper scans `wma->interfaces[i].vdev == NULL`, but the real allocator uses
the psoc's locked `wlan_vdev_id_map`. The helper never sets that map and never
places a host vdev in `wlan_vdev_list`. Another interface operation can select
the same ID while firmware still owns it.

This is a structural problem. Creating a real host-managed vdev is the clean
solution. Do not patch the private bitmap directly unless a supported reserve
API and complete object lifecycle are added; a one-off bit set would merely
move the inconsistency elsewhere.

### 5.8 Peer/address semantics likely block the beacon test

The ghost helper has one peer: its derived helper MAC. Stock management TX
first looks up a peer by destination address, then source address, then MLD
address. For a broadcast beacon/probe request, destination lookup cannot match;
source address becomes important.

The current patch rewrites source address only for broadcast probe requests.
It does not normalize beacon SA/BSSID. If `beacon_inject` uses the monitor MAC
or a spoofed address, it will not match the ghost self-peer. It is therefore
plausible that firmware discards the frame even after helper start. This is an
**inference**, because firmware peer selection is not open source.

Silently rewriting addresses is not a general solution. The implementation
must choose and document one contract:

- preserve arbitrary SA/BSSID and prove firmware accepts it;
- support only the vdev/self-peer MAC and reject other addresses;
- or manage real transient peers per source MAC, including their response,
  resource, teardown, and recovery lifecycle.

The third option is high risk and should not be the first milestone.

### 5.9 Channel state is fabricated or used after failure

The current monitor open path starts a helper at hard-coded 2412 MHz before a
successful user channel transition. After `wlan_hdd_set_mon_chan()`, it starts
or reconfigures the helper even when the real monitor VDEV_START timed out or
failed. A helper is not independent of the radio's actual channel.

The helper also fabricates 20 MHz 11G/11A PHY settings and 20 dBm power. It
does not faithfully carry channel width, center frequencies, regulatory power,
6 GHz rules, or puncturing.

Correct order:

```text
stop monitor TX queue
  -> drain/quarantine TX safely
  -> tear down old helper completely
  -> perform the stock validated monitor channel transition
  -> require successful monitor response
  -> derive settings from the resulting wlan_channel
  -> create/start a helper only if one is still needed
  -> wake TX queue
```

For the first milestone, reject anything outside 2.4/5 GHz 20 MHz rather than
guessing parameters.

### 5.10 The radiotap and netdev contracts are incomplete

The patch validates only radiotap version and length, then strips the entire
header. It ignores extended presence bitmaps, alignment, FCS-present, TX flags,
NOACK, rate, MCS/VHT/HE fields, and channel disagreement. An FCS included by
userspace may be transmitted as payload.

Use the kernel radiotap iterator if it is available/exported to this module, or
write a bounded parser following the same alignment rules. The initial contract
should accept a minimal header, strip an included FCS when explicitly flagged,
and reject unsupported TX-control fields rather than pretending to honor them.

`ndo_start_xmit` runs in atomic/BH-disabled contexts and cannot sleep. Normal
resource exhaustion should stop the netdev queue before it fills and wake it on
completion. If returning `NETDEV_TX_BUSY`, the driver must not retain, map, or
free the skb. If it returns `NETDEV_TX_OK` after a drop, count the reason; a
userspace `send()` success only means the kernel accepted the skb.

### 5.11 `null` is not the current injection endpoint

The current patch creates a firmware-only helper and no visible `null` netdev.
Advice to run `ip link set null up` or inject through `null` applies to an older
design. An unassociated STA netdev also cannot simply be treated as a raw
injection endpoint by bringing it UP.

## 6. Preferred experiment: use the real monitor vdev

This is the most important new direction from the pinned-source audit.

The stock OnePlus driver already does all of the following for the monitor
interface:

- creates a real object-manager vdev and reserves its ID;
- maps `QDF_MONITOR_MODE` to `WMI_VDEV_TYPE_MONITOR`;
- declares that monitor vdevs use a self-peer;
- creates the WMI/objmgr/DP peer through `wma_create_peer()`;
- registers monitor DP TX/RX operations;
- starts the vdev on a validated channel and processes its real response;
- exposes the standard management-TX descriptor pool and completion path.

The open-source code does not show that `WMI_MGMT_TX_SEND` rejects a monitor
vdev. That claim comes from third-party comments/reverse engineering and may or
may not match `peach-v2` firmware. Test it before maintaining a second vdev.

### 6.1 Proposed one-frame proof

Implement only enough future code to submit one management nbuf through:

```c
wlan_mgmt_txrx_mgmt_frame_tx(monitor_self_peer, ...)
```

Do not call `wmi_mgmt_unified_cmd_send()` directly and do not allocate a custom
descriptor. `wlan_mgmt_txrx_mgmt_frame_tx()` obtains a stock descriptor, holds
the peer reference, routes through `wma_mgmt_unified_cmd_send()`, and lets the
existing WMA completion code unmap before completion/free.

For the first proof, require `wmi_service_mgmt_tx_wmi`. The alternate HTT/CDP
management path has different download/OTA callback semantics and needs its own
ownership audit if this service is absent. Fill a zeroed `wmi_mgmt_params` from
validated state, approximately:

```c
mgmt.tx_frame = nbuf;
mgmt.pdata = qdf_nbuf_data(nbuf);
mgmt.frm_len = qdf_nbuf_len(nbuf);
mgmt.vdev_id = wlan_vdev_get_id(mon_vdev);
mgmt.chanfreq = active_monitor_freq;
mgmt.qdf_ctx = wlan_psoc_get_qdf_dev(psoc); /* or the existing WMA qdf_dev */
mgmt.tx_params_valid = false;
mgmt.tx_flags = 0;
mgmt.mlo_link_agnostic = false;
```

Do not assign `mgmt.desc_id`; `wma_mgmt_unified_cmd_send()` replaces it with the
descriptor allocated by the stock management component. Keep default rate
selection for the first proof and add radiotap rate control only after OTA works.

Submission requirements:

1. Resolve the active monitor `wlan_objmgr_vdev` by vdev ID with a proper ref.
2. Resolve that vdev's real self-peer using its link/self MAC, not the injected
   frame's DA/SA. Passing the peer explicitly avoids the host lookup gate in
   `wma_tx_frame`; firmware may still enforce address matching, which the test
   will reveal.
3. Use the actual successful monitor channel. Do not fall back to 2412.
4. Transfer one linear nbuf with radiotap removed. On synchronous failure the
   caller still owns/frees it. On success the management-TX path owns it.
5. Either pass no callbacks and let the stock handler free it, or pass exactly
   one completion callback that records status and frees it exactly once. If a
   callback is installed, its context must outlive vdev drain/SSR.
6. Log the firmware completion enum:
   `COMPLETE_OK`, `DISCARD`, `INSPECT`, `COMPLETE_NO_ACK`, or unknown.
7. Capture on an independent adapter. A host completion is evidence, not proof
   of RF airtime. Broadcast frames naturally receive no 802.11 ACK.

First use a valid probe request or beacon whose SA/BSSID equals the monitor
self-peer MAC. Then repeat with a deliberately different SA. This separates
“monitor vdev rejected” from “source peer missing.”

### 6.2 Ownership table for this path

| Point | skb/nbuf owner | DMA state |
|---|---|---|
| `ndo_start_xmit` entry | monitor TX function | unmapped |
| stock mgmt submit fails | monitor TX function | WMI has already unmapped if it mapped |
| stock mgmt submit succeeds | mgmt_txrx descriptor | mapped on LL/WMI path |
| WMI completion enters WMA | mgmt_txrx descriptor | mapped |
| after `wma_mgmt_unmap_buf` | completion path | unmapped |
| stock handler/no-callback or one custom callback | freed exactly once | unmapped |

No timeout reaper should be added. If even a one-frame proof gets no completion,
stop and diagnose the firmware path; do not make the missing completion “go
away” by freeing the buffer.

### 6.3 Result-driven decision

| Completion/OTA result | Interpretation | Next action |
|---|---|---|
| completion + correct OTA frame | monitor path works | build on the stock pipeline; delete ghost helper |
| `DISCARD`, no OTA | firmware/path rejected it | compare matching vs spoofed SA; then test helper |
| `NO_ACK`, frame visible | normal for some broadcast/unicast cases | treat OTA capture as success |
| completion says OK, no OTA | receiver/channel/FCS or firmware ambiguity | repeat with two receivers and inspect bytes/channel |
| no completion | unsupported/broken path or recovery | single-frame logs, WMI event tracing, no retries/reaper |
| synchronous send failure | host-side gate | log exact QDF status and failing function |

## 7. Second choice: a real host-managed helper

If the real monitor vdev is demonstrably rejected, prefer a real internal
helper over a ghost vdev.

The most promising candidate is `QDF_P2P_DEVICE_MODE` because the pinned MLME
maps it to AP type plus P2P-device subtype, and `mlme_vdev_uses_self_peer()`
returns true for that subtype. The normal lifecycle then reserves a vdev ID,
creates host/DP/WMI state and the self-peer, owns response timers, and drains
management descriptors during recovery.

This is still an experiment, not a proven fix:

- the helper must be internal and not exposed as a fake `null` interface;
- standalone monitor mode currently rejects normal cfg80211 concurrency, so
  policy-manager interactions need explicit review;
- some configurations redirect P2P-device operations to a STA vdev on vdev
  exhaustion; the helper must verify it obtained a distinct vdev;
- P2P-device is AP-type internally. The claim that any AP-like helper triggers
  beacon-offload firmware assertions is unproven for this firmware, so test
  create/start/stop/delete with zero TX first;
- use normal MLME/ROC/channel APIs, not direct WMI calls mixed with a host vdev.

Relevant normal creation sequence:

```text
wlan_objmgr_vdev_obj_create
  -> component create handlers / MLME object and state machine
  -> vdev_mgr_create_send
  -> CDP vdev attach
  -> sme_vdev_post_vdev_create_setup
  -> wma_vdev_self_peer_create
```

Do not manually duplicate only selected calls from this sequence. Partial
object-manager, WMA, and DP state is harder to recover than the current ghost.

Acceptance gate before TX: 100 create/start/stop/delete cycles across channel 1
and channel 36, with every expected response received, no timeout, no CNSS
recovery, no leaked vdev/peer, and the original monitor still operational.

## 8. Last resort: requirements for a firmware-only helper

If device evidence proves that only a STARTED STA ghost vdev can transmit, the
minimum defensible design is below.

### 8.1 Lifecycle state machine

```text
DOWN
  -> CREATING (create_sent)
  -> STARTING (wait START response + status)
  -> PEER_CREATING (wait response when service exists)
  -> READY

READY
  -> QUIESCING (block queue, drain active submitters)
  -> PEER_DELETING (wait when sync-delete service exists)
  -> STOPPING (wait STOP response)
  -> DELETING (wait DELETE response)
  -> DOWN

any response timeout/unexpected status
  -> FAILED_RECOVERY_REQUIRED
```

Keep phase flags independent of the high-level state so partial setup can be
unwound. A failure must not clear the vdev ID, descriptor tombstones, or mapped
buffers and then reuse them. On an uncertain timeout, quarantine them until a
known firmware reset.

### 8.2 Response interception

Restore the concept used in repository commit `4b004b9`:

- intercept helper VDEV_START before the stock response-timer lookup;
- intercept helper VDEV_STOP before its stock lookup;
- intercept helper VDEV_DELETE before its stock lookup;
- intercept peer-create confirmation before `WMA_PEER_CREATE_REQ` lookup;
- intercept peer-delete response before `WMA_DELETE_STA_REQ` lookup.

Each hook must first verify all of:

- injection feature initialized;
- current lifecycle phase expects this event;
- exact helper vdev ID;
- exact helper MAC for peer events;
- current helper generation/session.

Return “consumed” only on a full match. Copy status under a spinlock, then signal
a qdf event. Never wait while holding the spinlock.

### 8.3 TX slot state and teardown

A slot needs at least:

```text
FREE
LOCAL_OR_SUBMITTING
FW_OWNED
COMPLETING
QUARANTINED
```

Recommended submit algorithm:

1. Under `state_lock`, verify READY/channel/generation, reserve a collision-free
   descriptor and slot, copy all helper fields, increment active submitters.
2. Drop the lock and call WMI.
3. Reacquire the lock. On local failure, remove the still-local slot; the WMI
   failure path has already unmapped. On success, mark it FW_OWNED unless the
   completion hook already consumed it.
4. Decrement active submitters and signal the drain event if zero.

Completion must atomically detach the matching slot, then unmap/free outside the
spinlock. It must handle both single and bundle completion events; the stock
bundle handler already funnels each report through
`wma_process_mgmt_tx_completion()`.

Teardown order:

```text
stop netdev queue / reject new TX
  -> wait active_submitters == 0
  -> wait for FW-owned TX completions
  -> if TX remains: quarantine and request recovery; do not free
  -> peer delete as applicable
  -> vdev stop + response
  -> vdev delete + response
  -> release ID/state only after confirmed quiescence
```

A successful stop/delete response might be usable as a quiescence boundary,
but that is not documented in the available ABI. Prove it with targeted stress
and SMMU instrumentation before relying on it. Firmware reset/recovery is the
conservative boundary.

### 8.4 Vdev ID reservation remains the blocker

Response hooks do not solve ID collision. Without a supported object-manager
reservation, a ghost helper cannot be considered production-safe. At minimum,
all host vdev creation/destruction and helper lifecycle would need one shared
allocator/lock and a reservation visible to the real allocator. The pinned
source exposes no simple public “reserve this ID without a vdev object” flow.
This is the strongest reason to prefer a real helper.

## 9. Paths that should not be pursued first

### 9.1 `WMI_PDEV_FRAME_INJECT_CMDID`

Despite its name, the pinned host ABI does not carry an arbitrary frame buffer.
`wmi_host_injector_frame_params` contains only vdev ID, enable, predefined frame
type, period, duration, bandwidth, and destination MAC. It configures periodic
firmware-generated injector frames; it is not a general radiotap/raw-frame TX
path.

### 9.2 `WMI_OFFCHAN_DATA_TX_SEND_CMDID`

The wrapper and command encoder exist, but the pinned tree has no caller and no
registered completion handler for it. The extraction function is only an ops
pointer declaration. The encoder also ignores DMA-map failure and fails to
unmap on later command failure. Its existence does not prove `peach-v2`
firmware support or suitability for raw injection. It likely requires a real
vdev/peer/remain-on-channel lifecycle.

Do not expand to data frames until management injection has correct lifecycle,
completion, and OTA evidence.

### 9.3 QoS-null, packet capture, or `dev_queue_xmit()` alone

- QoS-null WMI sends a specific firmware construct, not arbitrary frames.
- packet-capture components observe RX/TX already handled by the datapath; they
  do not create a raw injection route.
- `dev_queue_xmit()` only reaches the driver's `ndo_start_xmit`; it cannot make
  an unsupported firmware vdev/path transmit.

### 9.4 More sleeps or a longer reaper timeout

Longer sleeps merely make races less frequent. A 30-second DMA reaper is still
unsafe if firmware owns the mapping. Only an event or a proven quiescence/reset
boundary changes ownership.

## 10. Review of the Loukious SM8150 commit

Reference supplied by the user:
`Loukious/android_kernel_xiaomi_sm8150@65c6a05ecd9b25ebf0742d39987c6a8a042227f1`.

Useful ideas:

- confirms one experimental family of patches used a helper STA and direct WMI
  management TX;
- points to the relevant WMI command and completion hook;
- highlights source-peer matching as a possible firmware gate;
- demonstrates the need to tear a helper down before monitor teardown.

It is **not a gold-standard implementation** for OnePlus 13:

- it targets an SM8150 kernel tree, not the pinned SM8750/`peach-v2` stack;
- it is a 45-file, 11,825-line change with many unrelated/testing additions;
- its helper uses fixed sleeps and an unreserved firmware-only vdev ID;
- its descriptor allocator has the same unaligned-base/mask bug;
- it states that firmware never completes helper TX and then unmaps/frees
  DMA-owned buffers after two seconds, which violates DMA lifetime rules;
- comments alternate between “hidden AP” and actual STA behavior and include
  reverse-engineered firmware-symbol assertions that are not portable evidence.

Use it as a hypothesis source only. Do not copy its reaper, allocator, or
lifecycle.

## 11. Future implementation map

No edits are requested now. If another agent implements the research, likely
touch points are:

| Area | File/function | Purpose |
|---|---|---|
| monitor netdev TX | `qcacld-3.0/core/hdd/src/wlan_hdd_main.c`, `wlan_mon_drv_ops` | add safe `ndo_start_xmit` |
| radiotap parsing | new small HDD helper or existing injection file | bounded parse and explicit supported fields |
| stock-path submission | WMA helper near `wma_mgmt_unified_cmd_send` or a converged mgmt helper | resolve real vdev/self-peer and call `wlan_mgmt_txrx_mgmt_frame_tx` |
| completion/status | one WMA-owned callback or existing WMA completion instrumentation | count status and free once |
| monitor channel gate | `wlan_hdd_set_mon_chan` | enable TX only after successful real channel transition |
| ghost response hooks, only if needed | `target_if_vdev_mgr_rx_ops.c`, `wma_dev_if.c` | consume exact helper lifecycle events |
| build integration | `wlan_qcacld3_modules.bzl` and current patch generation | compile only the selected design |

Avoid a large framework in the first build. The first patch should answer one
question: **Can the real monitor vdev submit one peer-matching management frame
through the stock management pipeline and produce a completion/OTA frame?**

## 12. Required instrumentation

Use bounded counters and rate-limited logs, not a log per high-rate frame.

Capability/lifecycle once per session:

- source/build revision and injection design (`real-monitor`, `real-helper`, or
  `ghost-helper`);
- `wmi_service_mgmt_tx_wmi`;
- `wmi_service_peer_create_conf`;
- `wmi_service_sync_delete_cmds`;
- monitor/helper vdev ID, WMI type/subtype, host state, MAC, actual channel and
  width;
- peer lookup success and peer MAC;
- every lifecycle command send result and response status/latency;
- SSR/recovery generation.

TX counters:

```text
accepted
malformed_radiotap
unsupported_radiotap
unsupported_frame_type
channel_not_ready
peer_missing
descriptor_exhausted
wmi_submit_failed
completion_ok
completion_discard
completion_no_ack
completion_unknown
completion_missing
ota_verified (userspace test result, not a kernel guess)
queue_stops / queue_wakes
quarantined_dma_buffers
```

Do not increment `tx_packets` merely because WMI accepted a command. Count
“submitted” separately from “completed”; OTA verification remains external.

## 13. On-device test matrix

Use an independent adapter placed close to the phone, locked to the exact
channel/width, with capture timestamps. Start at one frame per second.

### Gate A: stock monitor lifecycle

1. Load `qca_cld3_peach_v2.ko con_mode=4`.
2. Set channel 1, 20 MHz through the supported `iw` operation.
3. Require successful VDEV_START and log actual bss/des channel.
4. Verify monitor self-peer exists in objmgr/DP and record its MAC.
5. Do not create a helper yet.

### Gate B: real-monitor TX proof

Run these one at a time:

1. one valid broadcast probe request, SA = monitor self MAC;
2. one valid beacon, SA/BSSID = monitor self MAC;
3. one action frame with a known receiver;
4. repeat the probe/beacon with a different source address.

For every frame correlate:

- userspace send result;
- driver submission result and descriptor;
- firmware completion status;
- independent capture and actual frame bytes;
- CNSS/WMI/SMMU logs.

Repeat on channel 36 at 20 MHz only after channel 1 is stable.

### Gate C: lifecycle robustness

After a path produces OTA frames:

- 100 frames at 1/s, then 10/s, then 100/s;
- stop interface while one frame is in flight;
- channel 1 -> 36 -> 1 while TX is idle, then while queued;
- 100 interface down/up cycles;
- 20 module unload/reload cycles if the platform safely supports it;
- trigger the supported firmware recovery test and verify no old completion is
  matched to a new descriptor/generation;
- check that all descriptors, peer refs, DMA mappings, queues, vdev IDs, and
  wake locks return to baseline.

Do not advance the rate after a missing completion, negative counter, unknown
descriptor, SMMU fault, WMI timeout, or recovery.

## 14. Acceptance criteria

A build should be called “management-frame injection working” only when all are
true:

- the monitor channel operation succeeds through the normal state machine;
- at least probe request, beacon, and action management subtypes are captured
  OTA on 2.4 and 5 GHz 20 MHz;
- completion status and external capture correlate over repeated tests;
- the stated SA/BSSID contract is enforced (arbitrary or self-MAC-only);
- no buffer is timer-freed while firmware may own its DMA mapping;
- no duplicate/private descriptor ambiguity exists;
- interface stop/channel change/reload drain or quarantine safely;
- no leaked host or firmware vdev/peer/descriptor remains;
- no CNSS recovery, WMI timeout, SMMU fault, or kernel warning occurs in the
  stress matrix;
- unsupported radiotap fields, frame types, bands, and widths fail explicitly.

Passing compilation or seeing `send()` return success is not an acceptance
criterion.

## 15. Open questions that only device evidence can answer

1. Does `peach-v2` firmware accept `WMI_MGMT_TX_SEND` on the real monitor vdev?
2. What completion status is returned for the current missing beacon?
3. Does matching beacon/probe SA to the monitor/helper self-peer change it?
4. Does firmware emit management completions for a ghost STARTED STA vdev?
5. Which service bits are actually advertised on this firmware build?
6. Can a real P2P-device vdev start alongside standalone monitor without policy
   rejection or firmware assert?
7. Does successful STOP/DELETE guarantee outstanding management DMA is no
   longer referenced on this target?
8. Which fields (sequence, duration, rate, FCS) does firmware rewrite?

Do not encode answers to these as comments until a log/capture establishes
them.

## 16. Primary references

Pinned OnePlus/Qualcomm source:

- [OnePlus SM8750 WLAN tree at the exact pinned revision](https://github.com/OnePlusOSS/android_kernel_modules_and_devicetree_oneplus_sm8750/tree/d50b305f7da9e14715a25120a4ac7b1a4b8b97c3/vendor/qcom/opensource/wlan)
- [`send_mgmt_cmd_tlv`: mapping, physical address, send-failure unmap](https://github.com/OnePlusOSS/android_kernel_modules_and_devicetree_oneplus_sm8750/blob/d50b305f7da9e14715a25120a4ac7b1a4b8b97c3/vendor/qcom/opensource/wlan/qca-wifi-host-cmn/wmi/src/wmi_unified_tlv.c#L5244-L5355)
- [`wmi_mgmt_params`: pinned 16-bit descriptor API](https://github.com/OnePlusOSS/android_kernel_modules_and_devicetree_oneplus_sm8750/blob/d50b305f7da9e14715a25120a4ac7b1a4b8b97c3/vendor/qcom/opensource/wlan/qca-wifi-host-cmn/wmi/inc/wmi_unified_param.h#L1902-L1964)
- [Stock management descriptor allocation](https://github.com/OnePlusOSS/android_kernel_modules_and_devicetree_oneplus_sm8750/blob/d50b305f7da9e14715a25120a4ac7b1a4b8b97c3/vendor/qcom/opensource/wlan/qca-wifi-host-cmn/umac/cmn_services/mgmt_txrx/core/src/wlan_mgmt_txrx_main.c#L31-L85)
- [The pinned qcacld default of 64 management descriptors](https://github.com/OnePlusOSS/android_kernel_modules_and_devicetree_oneplus_sm8750/blob/d50b305f7da9e14715a25120a4ac7b1a4b8b97c3/vendor/qcom/opensource/wlan/qcacld-3.0/configs/config_to_feature.h#L2572-L2576)
- [Stock management submit/ownership path](https://github.com/OnePlusOSS/android_kernel_modules_and_devicetree_oneplus_sm8750/blob/d50b305f7da9e14715a25120a4ac7b1a4b8b97c3/vendor/qcom/opensource/wlan/qca-wifi-host-cmn/umac/cmn_services/mgmt_txrx/dispatcher/src/wlan_mgmt_txrx_utils_api.c#L458-L574)
- [Stock descriptor completion and bounds checks](https://github.com/OnePlusOSS/android_kernel_modules_and_devicetree_oneplus_sm8750/blob/d50b305f7da9e14715a25120a4ac7b1a4b8b97c3/vendor/qcom/opensource/wlan/qca-wifi-host-cmn/umac/cmn_services/mgmt_txrx/dispatcher/src/wlan_mgmt_txrx_tgt_api.c#L1625-L1730)
- [Vdev response handlers and their timer/object requirements](https://github.com/OnePlusOSS/android_kernel_modules_and_devicetree_oneplus_sm8750/blob/d50b305f7da9e14715a25120a4ac7b1a4b8b97c3/vendor/qcom/opensource/wlan/qca-wifi-host-cmn/target_if/mlme/vdev_mgr/src/target_if_vdev_mgr_rx_ops.c#L384-L600)
- [Object-manager vdev ID allocation/reservation](https://github.com/OnePlusOSS/android_kernel_modules_and_devicetree_oneplus_sm8750/blob/d50b305f7da9e14715a25120a4ac7b1a4b8b97c3/vendor/qcom/opensource/wlan/qca-wifi-host-cmn/umac/cmn_services/obj_mgr/src/wlan_objmgr_psoc_obj.c#L879-L914)
- [Monitor/P2P vdev type mapping](https://github.com/OnePlusOSS/android_kernel_modules_and_devicetree_oneplus_sm8750/blob/d50b305f7da9e14715a25120a4ac7b1a4b8b97c3/vendor/qcom/opensource/wlan/qcacld-3.0/components/mlme/core/src/wlan_mlme_vdev_mgr_interface.c#L1504-L1568)
- [Monitor and P2P-device self-peer rules](https://github.com/OnePlusOSS/android_kernel_modules_and_devicetree_oneplus_sm8750/blob/d50b305f7da9e14715a25120a4ac7b1a4b8b97c3/vendor/qcom/opensource/wlan/qcacld-3.0/components/mlme/core/src/wlan_mlme_vdev_mgr_interface.c#L2274-L2287)
- [Normal WMA self-peer creation](https://github.com/OnePlusOSS/android_kernel_modules_and_devicetree_oneplus_sm8750/blob/d50b305f7da9e14715a25120a4ac7b1a4b8b97c3/vendor/qcom/opensource/wlan/qcacld-3.0/core/wma/src/wma_dev_if.c#L2942-L2995)
- [Monitor link registration using the created self-peer](https://github.com/OnePlusOSS/android_kernel_modules_and_devicetree_oneplus_sm8750/blob/d50b305f7da9e14715a25120a4ac7b1a4b8b97c3/vendor/qcom/opensource/wlan/qcacld-3.0/core/hdd/src/wlan_hdd_tx_rx.c#L1315-L1362)
- [Peer-create confirmation and peer-delete response handlers](https://github.com/OnePlusOSS/android_kernel_modules_and_devicetree_oneplus_sm8750/blob/d50b305f7da9e14715a25120a4ac7b1a4b8b97c3/vendor/qcom/opensource/wlan/qcacld-3.0/core/wma/src/wma_dev_if.c#L3667-L3911)
- [Peer-create service-bit gating](https://github.com/OnePlusOSS/android_kernel_modules_and_devicetree_oneplus_sm8750/blob/d50b305f7da9e14715a25120a4ac7b1a4b8b97c3/vendor/qcom/opensource/wlan/qcacld-3.0/core/wma/src/wma_main.c#L6538-L6553)
- [Synchronous peer-delete service handling](https://github.com/OnePlusOSS/android_kernel_modules_and_devicetree_oneplus_sm8750/blob/d50b305f7da9e14715a25120a4ac7b1a4b8b97c3/vendor/qcom/opensource/wlan/qcacld-3.0/core/wma/src/wma_dev_if.c#L527-L585)
- [Normal management peer lookup and submission](https://github.com/OnePlusOSS/android_kernel_modules_and_devicetree_oneplus_sm8750/blob/d50b305f7da9e14715a25120a4ac7b1a4b8b97c3/vendor/qcom/opensource/wlan/qcacld-3.0/core/wma/src/wma_data.c#L2700-L2755)
- [Management completion status, unmap, and dispatch](https://github.com/OnePlusOSS/android_kernel_modules_and_devicetree_oneplus_sm8750/blob/d50b305f7da9e14715a25120a4ac7b1a4b8b97c3/vendor/qcom/opensource/wlan/qcacld-3.0/core/wma/src/wma_mgmt.c#L3020-L3210)
- [Management completion event registration](https://github.com/OnePlusOSS/android_kernel_modules_and_devicetree_oneplus_sm8750/blob/d50b305f7da9e14715a25120a4ac7b1a4b8b97c3/vendor/qcom/opensource/wlan/qcacld-3.0/core/wma/src/wma_main.c#L7043-L7070)
- [`WMI_PDEV_FRAME_INJECT_CMDID` host parameter shape](https://github.com/OnePlusOSS/android_kernel_modules_and_devicetree_oneplus_sm8750/blob/d50b305f7da9e14715a25120a4ac7b1a4b8b97c3/vendor/qcom/opensource/wlan/qca-wifi-host-cmn/wmi/inc/wmi_unified_param.h#L9216-L9235)

Kernel driver rules:

- [Linux dynamic DMA mapping guide](https://docs.kernel.org/core-api/dma-api-howto.html)
- [Linux netdev synchronization and `ndo_start_xmit` contract](https://docs.kernel.org/networking/netdevices.html)
- [Linux TX queue and stop/quiescence guidance](https://docs.kernel.org/networking/driver.html)
- [Linux radiotap parsing guidance](https://docs.kernel.org/networking/radiotap-headers.html)
- [mac80211 injection/radiotap semantics](https://docs.kernel.org/networking/mac80211-injection.html)

Third-party comparison, not authority:

- [Loukious SM8150 injection commit](https://github.com/Loukious/android_kernel_xiaomi_sm8150/commit/65c6a05ecd9b25ebf0742d39987c6a8a042227f1)
- [Its broken descriptor allocator](https://github.com/Loukious/android_kernel_xiaomi_sm8150/blob/65c6a05ecd9b25ebf0742d39987c6a8a042227f1/drivers/staging/qcacld-3.0/core/wma/src/wma_frame_inject.c#L568-L584)
- [Its unsafe timeout reaper](https://github.com/Loukious/android_kernel_xiaomi_sm8150/blob/65c6a05ecd9b25ebf0742d39987c6a8a042227f1/drivers/staging/qcacld-3.0/core/wma/src/wma_frame_inject.c#L918-L979)

## 17. Handoff checklist for the next agent

- [ ] Read this document and the exact pinned source before editing.
- [ ] Do not preserve the current timeout reaper or descriptor allocator.
- [ ] Make the first patch a real-monitor/stock-management-path proof.
- [ ] Log service bits, exact peer, submission status, completion status, and
      actual channel.
- [ ] Use a matching-SA frame first, then test arbitrary SA separately.
- [ ] Require an independent OTA capture.
- [ ] Keep DMA ownership explicit on every return and teardown path.
- [ ] Add a helper only after monitor-vdev rejection is demonstrated.
- [ ] Prefer a real object-manager helper; treat a ghost helper as recovery-
      sensitive experimental code.
- [ ] Run `./validate_workflow.sh` after future patch/workflow changes and do not
      trigger a live build until static validation passes.
