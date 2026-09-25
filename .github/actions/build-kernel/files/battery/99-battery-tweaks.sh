#!/system/bin/sh
# 99-battery-tweaks.sh - run by KernelSU ksud at boot, all zero-perf-cost
#
# Install (KernelSU never creates the directory itself):
#   mkdir -p /data/adb/service.d
#   cp 99-battery-tweaks.sh /data/adb/service.d/
#   chmod 755 /data/adb/service.d/99-battery-tweaks.sh
#
# page-cluster: the kernel default of 3 makes every swap-in fault read ahead
# 2^3 = 8 pages (32 KiB) from zram; Android's app-switch access pattern
# rarely touches them, so most of that decompression is thrown away. 0 makes
# each fault decompress exactly one page. Only set it from the known default.
[ "$(cat /proc/sys/vm/page-cluster 2>/dev/null)" = "3" ] && echo 0 > /proc/sys/vm/page-cluster

# oplus_log_level: the charger driver defaults to level 2 (spammy); 1 keeps
# error reporting while dropping the per-second informational log lines.
[ -w /sys/module/oplus_chg_v2/parameters/oplus_log_level ] && echo 1 > /sys/module/oplus_chg_v2/parameters/oplus_log_level
