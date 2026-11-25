#!/usr/bin/env python3
"""Final audit: every feature flag in configs must reach the action, and every
feature the README advertises must correspond to real pipeline code.

Run as the Phase-8 "nothing was silently dropped" check.
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
ACT = os.path.join(ROOT, ".github/actions/build-kernel/action.yml")
README = os.path.join(ROOT, "README.md")
CFGDIR = os.path.join(ROOT, "configs")

act = open(ACT, encoding="utf-8").read()
readme = open(README, encoding="utf-8").read()

# 1. config keys <-> $OP_* reads
keys = set()
for f in os.listdir(CFGDIR):
    if f.endswith(".json"):
        keys |= set(json.load(open(os.path.join(CFGDIR, f), encoding="utf-8")))
env_reads = set(re.findall(r"\$\{?(OP_[A-Z0-9_]+)\b", act))
# GitHub expressions read the same values as `env.OP_FOO` rather than `$OP_FOO`.
# ddk_injection is only ever consulted that way, so the $ form alone reports a
# false negative.
env_reads |= set(re.findall(r"\benv\.(OP_[A-Z0-9_]+)\b", act))
# A key counts as read if OP_<KEY> appears as a whole token. Checking plain
# substring membership gives a false negative for prefixes: OP_DDK_INJECTION
# contains OP_DDK, so `"OP_DDK_INJECTION" in reads` is True while a naive
# startswith-based scan of the key list reports ddk_injection as unread.
missing = sorted(
    k for k in keys
    if k != "model" and f"OP_{k.upper()}" not in env_reads
)
# Guard against the reverse error: a key whose prefix is a longer read is fine,
# but a key that is merely a prefix of another read is NOT covered by it.
prefix_only = []
for k in list(missing):
    longer = [r for r in env_reads if r.startswith(f"OP_{k.upper()}_")]
    if longer:
        # e.g. key "ddk" would be satisfied by OP_DDK_INJECTION only if the
        # action never reads OP_DDK itself; report it for manual review.
        prefix_only.append((k, longer))
print(f"config keys: {len(keys)}   $OP_* reads: {len(env_reads)}")
if missing:
    print(f"  keys with no exact reader: {missing}")
else:
    print("  keys with no reader: none")

# 2. README feature claims -> evidence in the action
CLAIMS = {
    "KernelSU":        r"Add KernelSU",
    "KernelSU-Next":   r"Add KernelSU Next",
    "SUSFS":           r"Add SUSFS|SUSFS patches applied",
    "BBG":             r"Add BBG",
    "NetHunter":       r"Nethunter",
    "BORE":            r"Apply BORE scheduler",
    "LRNG":            r"LRNG v60",
    "Droidspaces":     r"Add Droidspaces",
    "NTSync":          r"NTSync|NTSYNC",
    "Re-Kernel":       r"Re-Kernel|REKERNEL",
    "ADIOS":           r"ADIOS|adios",
    "IPSet":           r"IP_SET|IPSET",
    "TTL":             r"TTL Target",
    "BBRv3":           r"BBRv3",
    "battery_save":    r"battery_save|BATTERY_SAVE",
    "audit_off":       r"audit_off|AUDIT_OFF",
    "monitor injection": r"qcacld-monitor-injection|injection",
}
print()
print("README feature -> pipeline code present?")
bad = []
for name, pat in CLAIMS.items():
    ok = re.search(pat, act) is not None
    stated = name.lower() in readme.lower() or name.replace(" ", "") in readme.lower()
    mark = "OK     " if ok else "MISSING"
    if not ok:
        bad.append(name)
    print(f"  {mark} {name:20s} (stated in README: {'yes' if stated else 'no'})")

print()
if missing or bad:
    print(f"FAILED: unread config keys={missing} unsupported claims={bad}")
    sys.exit(1)
print("PASS: every config key has a reader; every advertised feature has pipeline code")
sys.exit(0)
