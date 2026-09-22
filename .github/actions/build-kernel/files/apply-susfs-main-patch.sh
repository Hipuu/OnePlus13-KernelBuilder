#!/usr/bin/env bash
# apply-susfs-main-patch.sh
#
# Apply the main SUSFS patch (50_add_susfs_in_<branch>.patch) safely.
#
# Why this script exists:
# The SUSFS patch expects context that some vendor kernels lack (extra headers,
# an expanded `if` block, etc.). The old approach mutated the source inline in
# action.yml, applied the SUSFS patch, then tried to revert the temporary
# mutations. That was fragile: a failure between mutate and revert could leave
# the tree in an inconsistent state.
#
# This script:
# 1. Applies a minimal prepatch to make the SUSFS patch context match.
# 2. Dry-runs the SUSFS patch.
# 3. If dry-run fails, runs cleanup and exits non-zero.
# 4. If dry-run succeeds, applies the SUSFS patch.
# 5. Runs any post-patch cleanup that is still required.
#
# Usage:
#   apply-susfs-main-patch.sh <ANDROID_VER> <KERNEL_VER> <SUSFS_KERNEL_BRANCH>
#
# Environment:
#   COMMON_KERNEL_FOLDER - current working directory must be the kernel tree
#
set -euo pipefail

ANDROID_VER="${1:-}"
KERNEL_VER="${2:-}"
SUSFS_KERNEL_BRANCH="${3:-}"

if [ -z "$ANDROID_VER" ] || [ -z "$KERNEL_VER" ] || [ -z "$SUSFS_KERNEL_BRANCH" ]; then
  echo "Usage: $0 <ANDROID_VER> <KERNEL_VER> <SUSFS_KERNEL_BRANCH>" >&2
  exit 1
fi

PATCH_FILE="${SUSFS_FOLDER:-susfs}/kernel_patches/50_add_susfs_in_${SUSFS_KERNEL_BRANCH}.patch"
if [ ! -f "$PATCH_FILE" ]; then
  echo "SUSFS main patch not found: $PATCH_FILE" >&2
  exit 1
fi

# Track which prepatch mutations this run performed, so cleanup can be exact.
PREPATCH_LOG=$(mktemp)
export PREPATCH_LOG
trap 'rm -f "$PREPATCH_LOG"' EXIT

log_prepatch() {
  echo "$1" >> "$PREPATCH_LOG"
}

# Idempotent prepatch: each mutation checks whether it is already present.
apply_prepatch() {
  echo "::group::SUSFS prepatch"

  if [ "$ANDROID_VER" = "android15" ] && [ "$KERNEL_VER" = "6.6" ]; then
    # Expand the if block so the SUSFS hunk matches the braced form.
    if grep -qF 'if (vma->vm_end > last_vma_end)' ./fs/proc/task_mmu.c && \
       ! grep -qxF 'SUSFS_IS_INODE_SUS_MAP' ./fs/proc/task_mmu.c; then
      echo "Prepatch: expanding last_vma_end if block for android15-6.6"
      perl -i -0pe 's/\t\t\tif \(vma->vm_end > last_vma_end\)\n\t\t\t\tsmap_gather_stats\(vma, &mss, last_vma_end\);/\t\t\tif (vma->vm_end > last_vma_end) {\n\t\t\t\tsmap_gather_stats(vma, \&mss, last_vma_end);\n\t\t\t\tlast_vma_end = vma->vm_end;\n\t\t\t}/' ./fs/proc/task_mmu.c
      log_prepatch "expanded_last_vma_end"
    fi

    # Add declarations the SUSFS patch context expects in pagemap_read.
    if ! grep -qxF $'\tunsigned int nr_subpages = __PAGE_SIZE / PAGE_SIZE;' ./fs/proc/task_mmu.c; then
      echo "Prepatch: adding nr_subpages/res declarations for android15-6.6"
      sed -i -e '/int ret = 0, copied = 0;/a \\tunsigned int nr_subpages \= __PAGE_SIZE \/ PAGE_SIZE;' \
             -e '/int ret = 0, copied = 0;/a \\tpagemap_entry_t \*res = NULL;' ./fs/proc/task_mmu.c
      log_prepatch "added_nr_subpages_res"
    fi

    # The SUSFS fs/exec.c hunk expects a `#ifndef __GENKSYMS__ / dma-buf.h`
    # block right after page_size_compat.h. Newer trees (6.6.118) ship it, but
    # older android15-6.6 patch levels (6.6.89/66/30/56) go straight to
    # <linux/uaccess.h>, so hunk #1 fails. Insert the block to match context.
    if ! grep -qxF '#include <linux/dma-buf.h>' ./fs/exec.c; then
      echo "Prepatch: adding __GENKSYMS__ dma-buf.h block to fs/exec.c for android15-6.6"
      awk '/#include <linux\/page_size_compat.h>/{print; print ""; print "#ifndef __GENKSYMS__"; print "#include <linux/dma-buf.h>"; print "#endif"; next} {print}' ./fs/exec.c > ./fs/exec.c.tmp && mv ./fs/exec.c.tmp ./fs/exec.c
      log_prepatch "added_genksyms_dmabuf_exec"
    fi

    # Add missing headers if absent.
    if ! grep -qxF '#include <linux/dma-buf.h>' ./fs/proc/base.c; then
      echo "Prepatch: adding dma-buf.h to fs/proc/base.c"
      sed -i '/#include <linux\/cpufreq_times.h>/a #include <linux\/dma-buf.h>' ./fs/proc/base.c
      log_prepatch "added_dma_buf_base"
    fi

    if ! grep -qxF '#include <linux/zswap.h>' ./mm/memory.c; then
      echo "Prepatch: adding zswap.h to mm/memory.c"
      sed -i '/#include <linux\/sched\/sysctl.h>/a #include <linux\/zswap.h>' ./mm/memory.c
      log_prepatch "added_zswap_memory"
    fi

    # Older android15-6.6 patch levels reference VMA_PAD_START without defining it.
    if grep -qF 'VMA_PAD_START(' ./fs/proc/task_mmu.c && ! grep -qF '#define VMA_PAD_START' ./fs/proc/task_mmu.c; then
      echo "Prepatch: inserting VMA_PAD_START fallback for android15-6.6"
      printf '#undef VMA_PAD_START\n#ifndef VMA_PAD_START\n#define VMA_PAD_START(vma) ((vma)->vm_end)\n#endif\n' > "$RUNNER_TEMP/vma_pad.txt"
      awk '/#include "internal.h"/{print; while((getline line < ENVIRON["RUNNER_TEMP"] "/vma_pad.txt") > 0) print line; close(ENVIRON["RUNNER_TEMP"] "/vma_pad.txt"); next} {print}' ./fs/proc/task_mmu.c > ./fs/proc/task_mmu.c.tmp && mv ./fs/proc/task_mmu.c.tmp ./fs/proc/task_mmu.c
      rm -f "$RUNNER_TEMP/vma_pad.txt"
      log_prepatch "vma_pad_fallback"
    fi
  fi

  if [ "$ANDROID_VER" = "android12" ] && [ "$KERNEL_VER" = "5.10" ]; then
    if ! grep -qxF $'\tif (!vma_pages(vma))' ./fs/proc/task_mmu.c; then
      echo "Prepatch: noting missing vma_pages guard for android12-5.10"
      log_prepatch "missing_vma_pages_510"
    fi
  fi

  if [ "$ANDROID_VER" = "android13" ] && [ "$KERNEL_VER" = "5.15" ]; then
    if ! grep -qxF $'\tif (!vma_pages(vma))' ./fs/proc/task_mmu.c; then
      echo "Prepatch: noting missing vma_pages guard for android13-5.15"
      log_prepatch "missing_vma_pages_515"
    fi

    if grep -qxF '#include <linux/swap_slots.h>' ./mm/memory.c; then
      echo "Prepatch: noting existing swap_slots.h for android13-5.15"
      log_prepatch "swap_slots_present_515"
    fi
  fi

  if [ "$ANDROID_VER" = "android14" ] && [ "$KERNEL_VER" = "6.1" ]; then
    if ! grep -qxF $'\tif (!vma_pages(vma))' ./fs/proc/task_mmu.c; then
      echo "Prepatch: noting missing vma_pages guard for android14-6.1"
      log_prepatch "missing_vma_pages_61"
    fi

    if ! grep -qxF '#include <linux/dma-buf.h>' ./fs/proc/base.c; then
      echo "Prepatch: adding dma-buf.h to fs/proc/base.c for android14-6.1"
      sed -i '/#include <linux\/cpufreq_times.h>/a #include <linux\/dma-buf.h>' ./fs/proc/base.c
      log_prepatch "added_dma_buf_base_61"
    fi
  fi

  echo "::endgroup::"
}

# Post-patch cleanup: only the mutations that are actually temporary.
apply_postpatch() {
  echo "::group::SUSFS postpatch cleanup"

  if [ "$ANDROID_VER" = "android15" ] && [ "$KERNEL_VER" = "6.6" ]; then
    if grep -qF 'if (vma->vm_end > last_vma_end) {' ./fs/proc/task_mmu.c && \
       ! grep -qxF 'SUSFS_IS_INODE_SUS_MAP' ./fs/proc/task_mmu.c; then
      echo "Postpatch: collapsing last_vma_end if block for android15-6.6"
      perl -i -0pe 's/\t\t\tif \(vma->vm_end > last_vma_end\) \{\n\t\t\t\tsmap_gather_stats\(vma, &mss, last_vma_end\);\n\t\t\t\tlast_vma_end = vma->vm_end;\n\t\t\t\}/\t\t\tif (vma->vm_end > last_vma_end)\n\t\t\t\tsmap_gather_stats(vma, \&mss, last_vma_end);/' ./fs/proc/task_mmu.c
    fi

    # The nr_subpages/res declarations are prepatched when the vendor kernel lacks
    # them, and the SUSFS patch's pagemap_read modifications reference both. Keep
    # them in place rather than reverting them, otherwise the build fails with
    # "use of undeclared identifier 'nr_subpages'".
  fi

  if [ "$ANDROID_VER" = "android12" ] && [ "$KERNEL_VER" = "5.10" ]; then
    if grep -qxF $'\t\t\tgoto show_pad;' ./fs/proc/task_mmu.c; then
      echo "Postpatch: changing goto show_pad to return 0 for android12-5.10"
      sed -i -e 's/goto show_pad;/return 0;/' ./fs/proc/task_mmu.c
    fi
  fi

  if [ "$ANDROID_VER" = "android13" ] && [ "$KERNEL_VER" = "5.15" ]; then
    if grep -qxF $'\t\t\tgoto show_pad;' ./fs/proc/task_mmu.c; then
      echo "Postpatch: changing goto show_pad to return 0 for android13-5.15"
      sed -i -e 's/goto show_pad;/return 0;/' ./fs/proc/task_mmu.c
    fi

    if ! grep -qxF '#include <linux/swap_slots.h>' ./mm/memory.c; then
      echo "Postpatch: re-adding swap_slots.h to mm/memory.c for android13-5.15"
      sed -i '/#include <linux\/vmalloc.h>/a #include <linux\/swap_slots.h>' ./mm/memory.c
    fi
  fi

  if [ "$ANDROID_VER" = "android14" ] && [ "$KERNEL_VER" = "6.1" ]; then
    if grep -qxF $'\t\t\tgoto show_pad;' ./fs/proc/task_mmu.c; then
      echo "Postpatch: changing goto show_pad to return 0 for android14-6.1"
      sed -i -e 's/goto show_pad;/return 0;/' ./fs/proc/task_mmu.c
    fi
  fi

  echo "::endgroup::"
}

# Dry-run the SUSFS main patch. Returns 0 if it would apply.
#
# Fuzz: we use patch's default fuzz factor (2) rather than --fuzz=0. This
# mirrors the upstream WildKernels/OnePlus_KernelSU_SUSFS flow, which applies
# 50_add_susfs_in_*.patch with a plain `patch -p1 --forward` (default fuzz).
# Older android15-6.6 patch levels (6.6.66/6.6.30/6.6.56) drift a few context
# lines around pure-addition hunks (e.g. show_smap in fs/proc/task_mmu.c and
# the include block in mm/memory.c); default fuzz absorbs that drift, whereas
# --fuzz=0 rejected those hunks outright. The prepatch still fixes the cases
# fuzz cannot (declarations the code references, structural if-block shape).
dry_run_patch() {
  patch -p1 --dry-run --forward < "$PATCH_FILE" >/dev/null 2>&1
}

# Main flow.
main() {
  cd "${COMMON_KERNEL_FOLDER:-.}"

  apply_prepatch

  echo "::group::SUSFS main patch dry-run"
  if dry_run_patch; then
    echo "SUSFS main patch dry-run succeeded"
    echo "::endgroup::"
  else
    echo "::error::SUSFS main patch dry-run failed after prepatch; see below for rejected hunk context"
    # Best-effort: show what failed (same fuzz as the real apply), then cleanup.
    patch -p1 --dry-run --forward < "$PATCH_FILE" || true
    apply_postpatch
    exit 1
  fi

  echo "::group::SUSFS main patch apply"
  # Apply with the same (default) fuzz as the dry-run above; see dry_run_patch.
  if patch -p1 --forward < "$PATCH_FILE"; then
    echo "SUSFS main patch applied"
    echo "::endgroup::"
  else
    echo "::error::SUSFS main patch apply failed after successful dry-run"
    apply_postpatch
    exit 1
  fi

  apply_postpatch
}

main "$@"
