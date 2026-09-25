#!/usr/bin/env python3
"""
Compile-and-check the injection reclaim path without a 40-minute DDK build.

`verify-by-compiling-not-by-applying` is the lesson this exists for: a patch
that applies cleanly is not a patch that compiles, and a green DDK build only
proves the files it reached.  The reclaim path is the one place where a mistake
is a use-after-free under firmware DMA, so it is worth checking in seconds.

What this does:
  1. Extracts the REAL text of three functions from the patched tree:
       hdd_mon_inject_quarantine_put
       hdd_mon_inject_slots_flush
       hdd_mon_inject_reaper
  2. Compiles them at -Werror against stub QDF/kernel primitives
     (driver-bodies-harness.c), so a type error, missing struct field, bad
     format specifier, or unbalanced spinlock fails here.
  3. Asserts the ownership invariants that make the design safe:
       - the reaper never unmaps and never frees
       - a quarantined buffer is freed exactly once, at teardown
       - overflow frees nothing and leaves the slot tracking its buffer
       - the flush array bound holds at the true worst case (32 + 32)

Usage:
    ./run.sh [path-to-patched-tree]

Default tree is /tmp/fixtree.  Exits non-zero on any failure, so it is
usable as a pre-commit gate.
"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TREE = sys.argv[1] if len(sys.argv) > 1 else "/tmp/fixtree"
SRC = os.path.join(TREE, "core/hdd/src/wlan_hdd_main.c")

FUNCS = {
    "quarantine_put": r"static bool hdd_mon_inject_quarantine_put\(qdf_nbuf_t nbuf\)\s*\{.*?\n\}",
    "slots_flush": r"static void hdd_mon_inject_slots_flush\(void\)\s*\{.*?\n\}",
    "reaper": r"static void hdd_mon_inject_reaper\(struct work_struct \*work\)\s*\{.*?\n\}\n",
}


def main():
    if not os.path.exists(SRC):
        sys.exit(f"ERROR: no such file: {SRC}")
    text = open(SRC).read()

    bodies = []
    for name, pat in FUNCS.items():
        m = re.search(pat, text, re.S)
        if not m:
            sys.exit(f"ERROR: could not extract {name} from {SRC}\n"
                     f"       (has the function been renamed or removed?)")
        bodies.append(m.group(0))
        print(f"  extracted {name}: {len(m.group(0))} chars")

    inc = os.path.join(HERE, "extracted.inc")
    with open(inc, "w") as f:
        f.write("\n\n".join(bodies) + "\n")

    exe = os.path.join(HERE, "driver-bodies-harness")
    cmd = ["gcc", "-O1", "-Wall", "-Wextra", "-Werror",
           "-Wno-unused-parameter", "-o", exe,
           os.path.join(HERE, "driver-bodies-harness.c")]
    print("  compiling at -Werror ...")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout, r.stderr)
        sys.exit("ERROR: the driver bodies do not compile")

    print("  running invariants ...")
    r = subprocess.run([exe], capture_output=True, text=True)
    print(r.stdout, end="")
    if r.returncode != 0:
        sys.exit("ERROR: invariant check failed")

    # Worst-case array bound under ASan/UBSan: 32 slots + 32 quarantined is
    # exactly the [SLOTS * 2] the flush declares.  A miscount here would be a
    # kernel stack overflow, so prove the bound rather than reason about it.
    print("  worst-case bounds under ASan/UBSan ...")
    asan = os.path.join(HERE, "bounds-asan")
    cmd = ["gcc", "-O1", "-g", "-fsanitize=address,undefined",
           "-Wall", "-Wextra", "-Wno-unused-parameter",
           "-Wno-unused-function", "-o", asan,
           os.path.join(HERE, "bounds-asan.c")]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout, r.stderr)
        sys.exit("ERROR: bounds harness does not compile")
    r = subprocess.run([asan], capture_output=True, text=True)
    print(r.stdout, end="")
    if r.returncode != 0:
        sys.exit("ERROR: bounds check failed")

    print("\nreclaim path OK: compiles at -Werror, invariants hold, bounds hold")


if __name__ == "__main__":
    main()
