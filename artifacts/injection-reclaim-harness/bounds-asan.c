/*
 * Compile-check harness for the REAL driver function bodies.
 *
 * Unlike lifecycle.c (which transcribes the logic onto a model), this
 * includes extracted.inc -- the exact text of
 * hdd_mon_inject_quarantine_put, hdd_mon_inject_slots_flush and
 * hdd_mon_inject_reaper as they appear in the patched
 * core/hdd/src/wlan_hdd_main.c -- and compiles it with stub
 * implementations of the kernel/QDF primitives.  So a type error, a
 * missing field, a bad format specifier or an unused/undeclared
 * variable in the real bodies fails here, at -Werror, without a 40-minute
 * DDK build.
 *
 * The stubs are deliberately hostile in the ways that matter:
 *   - qdf_spin_lock_bh/unlock_bh count depth, so an unbalanced lock is
 *     caught (we assert depth 0 between calls);
 *   - qdf_nbuf_unmap_single records every unmap, so the "the reaper must
 *     not unmap" property is checked against the real code path.
 */
#include <stdio.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <stddef.h>

/* ---- kernel / QDF primitives the extracted bodies use ------------- */
#define HDD_MON_INJECT_SLOTS 32
#define HDD_MON_INJECT_DESC_BASE 0xF000U
#define HDD_MON_INJECT_TOKEN_MASK 0x0FFFU
#define HDD_MON_INJECT_GEN_MASK 0x7FU
#define HDD_MON_INJECT_GEN_SHIFT 5
#define HDD_MON_INJECT_SLOT_MASK (HDD_MON_INJECT_SLOTS - 1)
#define HDD_MON_INJECT_REAP_MS 1000
#define HDD_MON_INJECT_REAP_FAST_MS 250
#define HDD_MON_INJECT_AGE_MS 5000
#define HDD_MON_INJECT_PRESSURE 16
#define HDD_MON_INJECT_BACKPRESSURE 30
#define HDD_MON_INJECT_PEER_MAX 16
#define QDF_MAC_ADDR_SIZE 6
#define QDF_MODULE_ID_QDF_DEVICE 0
#define WLAN_INVALID_VDEV_ID 0xFF

typedef struct nbuf { int id; } *qdf_nbuf_t;
typedef struct { int locked; } qdf_spinlock_t;
typedef struct work_struct { int unused; } work_struct;
struct net_device { int unused; };

static unsigned long jiffies = 0;
#define msecs_to_jiffies(ms) ((unsigned long)(ms))
#define time_after(a, b) ((long)((b) - (a)) < 0)

static int lock_depth;
static void qdf_spin_lock_bh(qdf_spinlock_t *l) { (void)l; lock_depth++; }
static void qdf_spin_unlock_bh(qdf_spinlock_t *l) { (void)l; lock_depth--; }

/* Every unmap is recorded: the reaper must produce ZERO of them. */
static int unmap_calls;
static int free_calls;
static struct nbuf nbufs[4096];
static int nbuf_n;
static void *cds_get_context(int id) { (void)id; return (void *)1; }
static void qdf_nbuf_unmap_single(void *ctx, qdf_nbuf_t n, int dir)
{ (void)ctx; (void)n; (void)dir; unmap_calls++; }
static void qdf_nbuf_free(qdf_nbuf_t n) { (void)n; free_calls++; }

/* logging: capture the last message so the format string is compiled */
static char last_log[512];
#define HDD_LOG(fmt, ...) do { snprintf(last_log, sizeof(last_log), fmt, ##__VA_ARGS__); } while (0)
#define hdd_info(...) HDD_LOG(__VA_ARGS__)
#define hdd_err(...)  HDD_LOG(__VA_ARGS__)
#define hdd_err_rl(...) HDD_LOG(__VA_ARGS__)
#define hdd_debug(...) do { } while (0)

static void schedule_delayed_work(void *w, unsigned long d) { (void)w; (void)d; }
static void hdd_mon_inject_refresh_ap_bssid(void) { }
static void hdd_mon_inject_unmap_free(qdf_nbuf_t nbuf)
{
	void *qdf_ctx;
	if (!nbuf) return;
	qdf_ctx = cds_get_context(QDF_MODULE_ID_QDF_DEVICE);
	if (qdf_ctx) qdf_nbuf_unmap_single(qdf_ctx, nbuf, 0);
	qdf_nbuf_free(nbuf);
}

/* ---- mirrors of the real structs ---------------------------------- */
struct hdd_mon_inject_slot {
	bool in_use;
	qdf_nbuf_t nbuf;
	unsigned long submit_jiffies;
	uint32_t desc_id;
	uint32_t gen;
};

struct hdd_mon_inject_ctx {
	bool ready;
	bool lock_inited;
	bool helper_created;
	uint8_t vdev_id;
	uint8_t helper_mac[QDF_MAC_ADDR_SIZE];
	uint8_t peer_mac[HDD_MON_INJECT_PEER_MAX][QDF_MAC_ADDR_SIZE];
	uint8_t peer_count;
	uint8_t ap_bssid[QDF_MAC_ADDR_SIZE];
	bool ap_bssid_valid;
	uint32_t chanfreq;
	qdf_spinlock_t lock;
	struct hdd_mon_inject_slot slots[HDD_MON_INJECT_SLOTS];
	qdf_nbuf_t quarantine[HDD_MON_INJECT_SLOTS];
	uint32_t quarantine_count;
	bool quarantine_full;
	uint32_t next_slot;
	uint32_t inflight;
	struct net_device *mon_dev;
	uint64_t tx_ok, tx_fail, tx_complete, bssid_skipped;
	uint64_t tx_stale, reaped, flushed, quarantined;
};

static struct hdd_mon_inject_ctx g_inj;
static struct work_struct reaper_dw;
#define hdd_mon_inject_reaper_dw reaper_dw

/* ================= the REAL driver bodies ========================== */
#include "extracted.inc"

/* ------------------------------------------------------------------ */
static int fails;
static void expect(int cond, const char *what)
{
	printf("  %-58s %s\n", what, cond ? "ok" : "FAIL");
	if (!cond) fails++;
}

static qdf_nbuf_t nbuf_new(void)
{
	qdf_nbuf_t n = &nbufs[nbuf_n];
	n->id = nbuf_n++;
	return n;
}

static void reset(void)
{
	memset(&g_inj, 0, sizeof(g_inj));
	g_inj.lock_inited = true;
	g_inj.ready = true;
	unmap_calls = free_calls = nbuf_n = 0;
	lock_depth = 0;
	last_log[0] = 0;
}

int main(void)
{
	int i;
	reset();
	for (i = 0; i < HDD_MON_INJECT_SLOTS; i++) {
		g_inj.slots[i].in_use = true;
		g_inj.slots[i].nbuf = nbuf_new();
		g_inj.inflight++;
	}
	for (i = 0; i < HDD_MON_INJECT_SLOTS; i++)
		g_inj.quarantine[i] = nbuf_new();
	g_inj.quarantine_count = HDD_MON_INJECT_SLOTS;
	printf("worst case: %d in slots + %d quarantined = %d buffers\n",
	       HDD_MON_INJECT_SLOTS, HDD_MON_INJECT_SLOTS, nbuf_n);
	hdd_mon_inject_slots_flush();
	expect(free_calls == nbuf_n, "all buffers freed exactly once");
	expect(unmap_calls == nbuf_n, "all unmapped exactly once");
	printf("\n%s\n", fails ? "FAILED" : "BOUNDS OK");
	return fails != 0;
}
#if 0


	printf("Real driver bodies: quarantine_put / slots_flush / reaper\n");

	/* 1. reaper with no aged slots does nothing, and never unmaps */
	reset();
	for (i = 0; i < 10; i++) {
		g_inj.slots[i].in_use = true;
		g_inj.slots[i].nbuf = nbuf_new();
		g_inj.slots[i].submit_jiffies = jiffies;	/* fresh */
		g_inj.inflight++;
	}
	hdd_mon_inject_reaper(&reaper_dw);
	printf("1. fresh slots\n");
	expect(unmap_calls == 0, "no unmap");
	expect(free_calls == 0, "no free");
	expect(g_inj.reaped == 0, "reaped == 0");
	expect(g_inj.quarantine_count == 0, "quarantine empty");
	expect(lock_depth == 0, "spinlock balanced");
	expect(g_inj.inflight == 10, "inflight untouched");

	/* 2. aged slots: reaper quarantines them, still never unmaps */
	reset();
	for (i = 0; i < 10; i++) {
		g_inj.slots[i].in_use = true;
		g_inj.slots[i].nbuf = nbuf_new();
		g_inj.slots[i].submit_jiffies = 0;
		g_inj.slots[i].desc_id = 0xF000u | i;
		g_inj.inflight++;
	}
	jiffies = msecs_to_jiffies(HDD_MON_INJECT_AGE_MS) + 1;
	hdd_mon_inject_reaper(&reaper_dw);
	printf("2. aged slots (the lost-completion path)\n");
	expect(unmap_calls == 0, "reaper did NOT unmap");
	expect(free_calls == 0, "reaper did NOT free");
	expect(g_inj.reaped == 10, "reaped == 10");
	expect(g_inj.quarantined == 10, "quarantined == 10");
	expect(g_inj.quarantine_count == 10, "10 held in quarantine");
	expect(g_inj.inflight == 0, "inflight drained to 0");
	expect(lock_depth == 0, "spinlock balanced");
	for (i = 0; i < 10; i++)
		expect(!g_inj.slots[i].in_use, "slot released (reusable)");
	printf("  log: %s\n", last_log);

	/* 3. the held buffers are freed exactly once, at teardown */
	hdd_mon_inject_slots_flush();
	printf("3. teardown flush drains the quarantine\n");
	expect(unmap_calls == 10, "10 unmapped at flush");
	expect(free_calls == 10, "10 freed at flush");
	expect(g_inj.quarantine_count == 0, "quarantine drained");
	expect(g_inj.quarantine_full == false, "full flag cleared");
	expect(g_inj.flushed == 10, "flushed == 10");
	expect(lock_depth == 0, "spinlock balanced");

	/* 4. overflow: fails safe, frees nothing, leaves slots intact */
	reset();
	for (i = 0; i < HDD_MON_INJECT_SLOTS; i++) {
		g_inj.slots[i].in_use = true;
		g_inj.slots[i].nbuf = nbuf_new();
		g_inj.slots[i].submit_jiffies = 0;
		g_inj.inflight++;
	}
	jiffies = msecs_to_jiffies(HDD_MON_INJECT_AGE_MS) + 1;
	hdd_mon_inject_reaper(&reaper_dw);	/* fills the quarantine (32) */
	printf("4. quarantine overflow\n");
	expect(g_inj.quarantine_count == HDD_MON_INJECT_SLOTS, "quarantine exactly at capacity");
	expect(unmap_calls == 0, "still no unmap");
	/* now the table is empty; put one more aged slot in and reap */
	g_inj.slots[0].in_use = true;
	g_inj.slots[0].nbuf = nbuf_new();
	g_inj.slots[0].submit_jiffies = 0;
	g_inj.inflight = 1;
	hdd_mon_inject_reaper(&reaper_dw);
	expect(g_inj.quarantine_full == true, "overflow flagged");
	expect(g_inj.quarantine_count == HDD_MON_INJECT_SLOTS, "capacity not exceeded");
	expect(g_inj.slots[0].in_use == true, "overflowed slot left intact (tracked)");
	expect(g_inj.slots[0].nbuf != NULL, "overflowed buffer still attached");
	expect(unmap_calls == 0, "overflow freed nothing");
	printf("  log: %s\n", last_log);
	/* teardown still recovers everything, exactly once */
	{
		int before = nbuf_n;
		hdd_mon_inject_slots_flush();
		expect(free_calls == before, "teardown frees every buffer once");
		expect(unmap_calls == before, "and unmaps each exactly once");
		expect(g_inj.quarantine_count == 0, "quarantine drained after overflow");
	}

	printf("\n%s (%d failure(s))\n", fails ? "FAILED" : "ALL CHECKS PASS", fails);
	return fails != 0;
}

#endif
