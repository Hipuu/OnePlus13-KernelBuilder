# Injection reclaim harness

Compile-and-check the driver's reclaim path in ~2 seconds instead of a ~40-minute DDK
build. Born from `verify-by-compiling-not-by-applying`: a patch that *applies* cleanly is
not a patch that *compiles*, and a green DDK build only proves the files it reached.

The reclaim path is the one place in this patch where a mistake is a use-after-free
under live firmware DMA (that is exactly what the 987.9s SMMU fault was), so it is worth
checking cheaply and often.

    ./run.sh [path-to-patched-tree]      # default: /tmp/fixtree

## What it checks

1. **Extracts the real function text** from the patched tree — not a transcription:
   `hdd_mon_inject_quarantine_put`, `hdd_mon_inject_slots_flush`,
   `hdd_mon_inject_reaper`. If one is renamed or removed, the harness fails loudly
   rather than silently checking nothing.
2. **Compiles it at `-Werror`** against stub QDF/kernel primitives
   (`driver-bodies-harness.c`), so a type error, a missing struct field, a bad format
   specifier or an unbalanced `qdf_spin_lock_bh` fails here.
3. **Asserts the ownership invariants** that make the design safe:
   - the reaper never unmaps and never frees (this is the whole point of the quarantine)
   - a buffer the reaper reclaimed is freed exactly once, at teardown
   - quarantine overflow frees nothing and leaves the slot tracking its buffer
   - a spinlock is balanced across every path
4. **Proves the array bound** (`bounds-asan.c`) under ASan/UBSan at the true worst case:
   32 slots in use *and* 32 quarantined is exactly the `nbufs[SLOTS * 2]` the flush
   declares. A miscount there would be a kernel stack overflow, so it is measured, not
   reasoned about.

## What it does NOT check

It is a model of the *reclaim logic*, not of the driver. It cannot see whether the patch
applied to the right source, whether the token layout matches what firmware echoes, or
whether the WMI calls are correct — those need the round-trip diff, the CI grep guards,
and ultimately the phone.

Passing this means "the reclaim logic compiles and its ownership rules hold", which is
the part that can be checked offline. It is a pre-commit gate, not a substitute for the
on-device test.
