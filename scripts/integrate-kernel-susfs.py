#!/usr/bin/env python3
"""Apply the pinned SUSFS Android 15 / 6.6 patch to a locked kernel tree.

The upstream patch expects a small amount of compatibility context that is
not identical across every OnePlus release.  This helper prepares only those
audited contexts, invokes GNU patch without a shell, restores temporary
matching aids, and records the exact resulting file hashes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any


BASES = {"oos15-cn", "oos15-global", "oos16"}
PATCH_RELATIVE = Path("kernel_patches/50_add_susfs_in_gki-android15-6.6.patch")
SUSFS_C_RELATIVE = Path("kernel_patches/fs/susfs.c")
HEADER_DIRECTORY_RELATIVE = Path("kernel_patches/include/linux")
STAMP_NAME = ".op13-susfs-kernel.json"

TRACE_FS = "#include <trace/hooks/fs.h>"
TRACE_BLK = "#include <trace/hooks/blk.h>"
DMA_BUF = "#include <linux/dma-buf.h>"
CPUFREQ_TIMES = "#include <linux/cpufreq_times.h>"
ZSWAP = "#include <linux/zswap.h>"
SCHED_SYSCTL = "#include <linux/sched/sysctl.h>"

TASK_MARKER = "\tint ret = 0, copied = 0;"
TASK_DECLARATIONS = (
    "\tunsigned int nr_subpages = __PAGE_SIZE / PAGE_SIZE;\n"
    "\tpagemap_entry_t *res = NULL;"
)
TASK_CONTEXT = f"{TASK_MARKER}\n{TASK_DECLARATIONS}"
LAST_VMA_COMPACT = (
    "\t\t\tif (vma->vm_end > last_vma_end)\n"
    "\t\t\t\tsmap_gather_stats(vma, &mss, last_vma_end);"
)
LAST_VMA_EXPANDED = (
    "\t\t\tif (vma->vm_end > last_vma_end) {\n"
    "\t\t\t\tsmap_gather_stats(vma, &mss, last_vma_end);\n"
    "\t\t\t\tlast_vma_end = vma->vm_end;\n"
    "\t\t\t}"
)


class IntegrationError(RuntimeError):
    """The source tree did not match the audited SUSFS integration contract."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_link_like(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(metadata.st_mode):
        return True
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse_flag and attributes & reparse_flag)


def _assert_no_link_components(raw: Path, label: str) -> None:
    absolute = Path(os.path.abspath(raw))
    parts = absolute.parts
    if not parts:
        raise IntegrationError(f"{label} path is empty")
    current = Path(parts[0])
    for part in parts[1:]:
        current /= part
        if _is_link_like(current):
            raise IntegrationError(f"{label} path contains a symlink or junction: {current}")


def _require_plain_directory(raw: Path, label: str) -> Path:
    _assert_no_link_components(raw, label)
    resolved = raw.resolve()
    if not resolved.is_dir():
        raise IntegrationError(f"{label} directory is missing: {resolved}")
    return resolved


def _relative_path(value: str, label: str) -> Path:
    pure = PurePosixPath(value)
    if not value or pure.is_absolute() or ".." in pure.parts or "\\" in value:
        raise IntegrationError(f"{label} escapes its root: {value!r}")
    if any(part in {"", "."} for part in pure.parts):
        raise IntegrationError(f"{label} is not canonical: {value!r}")
    return Path(*pure.parts)


def _assert_plain_components(root: Path, relative: Path, label: str, *, leaf_may_be_missing: bool = False) -> Path:
    candidate = root.joinpath(relative)
    current = root
    for index, part in enumerate(relative.parts):
        current = current / part
        is_leaf = index == len(relative.parts) - 1
        if _is_link_like(current):
            raise IntegrationError(f"{label} contains a symlink: {current}")
        if not current.exists() and not (is_leaf and leaf_may_be_missing):
            raise IntegrationError(f"{label} is missing: {current}")
    resolved = candidate.resolve()
    if not _inside(resolved, root):
        raise IntegrationError(f"{label} escapes its root: {candidate}")
    return resolved


def _require_plain_file(root: Path, relative: Path, label: str) -> Path:
    result = _assert_plain_components(root, relative, label)
    if not result.is_file():
        raise IntegrationError(f"{label} is not a regular file: {result}")
    return result


def _destination(root: Path, relative: Path, label: str) -> Path:
    parent = _assert_plain_components(root, relative.parent, f"{label} parent")
    result = _assert_plain_components(root, relative, label, leaf_may_be_missing=True)
    if result.exists() or result.is_symlink():
        raise IntegrationError(f"{label} already exists: {result}")
    if not parent.is_dir():
        raise IntegrationError(f"{label} parent is not a directory: {parent}")
    return result


def _read_lf_text(path: Path, label: str) -> str:
    data = path.read_bytes()
    if b"\x00" in data:
        raise IntegrationError(f"{label} contains a NUL byte: {path}")
    if b"\r" in data:
        raise IntegrationError(
            f"{label} does not use LF-only line endings: {path}; preserve source line endings when checking out"
        )
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise IntegrationError(f"{label} is not UTF-8: {path}") from exc


def _write_lf_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _patch_targets(patch_file: Path) -> list[Path]:
    text = _read_lf_text(patch_file, "SUSFS patch")
    if "new file mode 120000" in text or "old mode 120000" in text:
        raise IntegrationError("SUSFS patch contains a symlink mode")
    if any(
        line.startswith(("new file mode ", "deleted file mode "))
        or line in {"--- /dev/null", "+++ /dev/null"}
        for line in text.splitlines()
    ):
        raise IntegrationError("SUSFS patch creates or deletes a path")
    targets: list[Path] = []
    seen: set[str] = set()
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.startswith("diff --git "):
            continue
        fields = line.split()
        if len(fields) != 4 or not fields[2].startswith("a/") or not fields[3].startswith("b/"):
            raise IntegrationError(f"malformed diff header at line {line_number}")
        before = fields[2][2:]
        after = fields[3][2:]
        if before != after:
            raise IntegrationError(f"SUSFS patch renames a path at line {line_number}")
        relative = _relative_path(after, f"patch target at line {line_number}")
        normalized = relative.as_posix()
        if normalized in seen:
            raise IntegrationError(f"SUSFS patch repeats target {normalized}")
        seen.add(normalized)
        targets.append(relative)
    if not targets:
        raise IntegrationError("SUSFS patch has no file targets")
    return targets


def _residue(root: Path) -> list[Path]:
    matches: set[Path] = set()
    for suffix in (".rej", ".orig"):
        for path in root.rglob(f"*{suffix}"):
            if path.is_file() or path.is_symlink():
                matches.add(path)
    return sorted(matches, key=lambda item: item.relative_to(root).as_posix())


def _patch_utility() -> str:
    executable = shutil.which("patch")
    if executable:
        return executable
    git = shutil.which("git")
    if git:
        bundled = Path(git).resolve().parents[1] / "usr" / "bin" / "patch.exe"
        if bundled.is_file():
            return str(bundled)
    raise IntegrationError("GNU patch is required")


def _gnu_patch_version() -> str:
    result = subprocess.run(
        [_patch_utility(), "--version"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    first_line = result.stdout.splitlines()[0] if result.stdout else ""
    if result.returncode != 0 or "GNU patch" not in first_line:
        raise IntegrationError(f"unsupported patch implementation: {first_line!r}")
    return first_line


def _run_patch(tree: Path, patch_file: Path) -> tuple[int, str]:
    command = [
        _patch_utility(),
        "-p1",
        "--forward",
        "--batch",
        "--no-backup-if-mismatch",
        "--input",
        str(patch_file),
    ]
    try:
        result = subprocess.run(
            command,
            cwd=tree,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
    except FileNotFoundError as exc:
        raise IntegrationError("GNU patch is required") from exc
    return result.returncode, result.stdout


def _ensure_line_after(text: str, wanted: str, anchor: str, label: str) -> tuple[str, bool]:
    lines = text.splitlines()
    wanted_count = lines.count(wanted)
    if wanted_count == 1:
        return text, False
    if wanted_count != 0:
        raise IntegrationError(f"{label}: expected at most one {wanted!r} line, found {wanted_count}")
    anchor_count = lines.count(anchor)
    if anchor_count != 1:
        raise IntegrationError(f"{label}: expected one {anchor!r} anchor, found {anchor_count}")
    needle = anchor + "\n"
    if text.count(needle) != 1:
        raise IntegrationError(f"{label}: anchor is not followed by an LF newline")
    return text.replace(needle, needle + wanted + "\n", 1), True


def _prepare_task_mmu(text: str) -> tuple[str, bool, bool]:
    declaration_lines = TASK_DECLARATIONS.splitlines()
    declaration_counts = [text.splitlines().count(line) for line in declaration_lines]
    if text.count(TASK_CONTEXT) == 1 and declaration_counts == [1, 1]:
        task_inserted = False
    elif declaration_counts == [0, 0]:
        if text.splitlines().count(TASK_MARKER) != 1:
            raise IntegrationError("task_mmu compatibility marker is not unique")
        marker = TASK_MARKER + "\n"
        if text.count(marker) != 1:
            raise IntegrationError("task_mmu compatibility marker is not followed by LF")
        text = text.replace(marker, TASK_CONTEXT + "\n", 1)
        task_inserted = True
    else:
        raise IntegrationError("task_mmu nr_subpages/res declarations are partial or misplaced")

    expanded_count = text.count(LAST_VMA_EXPANDED)
    compact_count = text.count(LAST_VMA_COMPACT)
    if expanded_count == 1 and compact_count == 0:
        last_vma_expanded = False
    elif expanded_count == 0 and compact_count == 1:
        text = text.replace(LAST_VMA_COMPACT, LAST_VMA_EXPANDED, 1)
        last_vma_expanded = True
    else:
        raise IntegrationError(
            "task_mmu last_vma_end context is ambiguous "
            f"(compact={compact_count}, expanded={expanded_count})"
        )
    return text, task_inserted, last_vma_expanded


def _restore_task_mmu(text: str, *, task_inserted: bool, last_vma_expanded: bool) -> str:
    if task_inserted:
        if text.count(TASK_CONTEXT) != 1:
            raise IntegrationError("recorded task_mmu declaration context changed during patching")
        text = text.replace(TASK_CONTEXT, TASK_MARKER, 1)
    if last_vma_expanded:
        if text.count(LAST_VMA_EXPANDED) != 1:
            raise IntegrationError("recorded task_mmu last_vma_end context changed during patching")
        text = text.replace(LAST_VMA_EXPANDED, LAST_VMA_COMPACT, 1)
    return text


def _atomic_copy(source: Path, destination: Path) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        with source.open("rb") as input_handle, os.fdopen(descriptor, "wb") as output_handle:
            shutil.copyfileobj(input_handle, output_handle, length=1024 * 1024)
            output_handle.flush()
            os.fsync(output_handle.fileno())
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    if sha256_file(destination) != sha256_file(source):
        raise IntegrationError(f"copied SUSFS file hash mismatch: {destination}")


def _kernel_version(source: Path) -> str:
    makefile = _require_plain_file(source, Path("Makefile"), "kernel Makefile")
    text = _read_lf_text(makefile, "kernel Makefile")
    values: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if key in {"VERSION", "PATCHLEVEL"}:
            values[key] = value
    if values != {"VERSION": "6", "PATCHLEVEL": "6"}:
        raise IntegrationError(f"SUSFS android15-6.6 patch requires kernel 6.6, found {values!r}")
    return "6.6"


def _assert_symbols(source: Path, copied: list[tuple[Path, Path]]) -> list[str]:
    required = {
        Path("fs/Makefile"): "obj-$(CONFIG_KSU_SUSFS) += susfs.o",
        Path("fs/namespace.c"): "CONFIG_KSU_SUSFS_SUS_MOUNT",
        Path("fs/proc/task_mmu.c"): "CONFIG_KSU_SUSFS_SUS_MAP",
        Path("fs/susfs.c"): "CONFIG_KSU_SUSFS_SUS_PATH",
        Path("include/linux/susfs.h"): '#define SUSFS_VERSION "',
        Path("include/linux/susfs_def.h"): "SUSFS_IS_INODE_SUS_MAP",
    }
    verified: list[str] = []
    for relative, token in required.items():
        path = _require_plain_file(source, relative, f"SUSFS output {relative.as_posix()}")
        text = _read_lf_text(path, f"SUSFS output {relative.as_posix()}")
        if token not in text:
            raise IntegrationError(f"SUSFS output {relative.as_posix()} lacks required symbol {token!r}")
        verified.append(token)
    for source_file, destination in copied:
        if sha256_file(source_file) != sha256_file(destination):
            raise IntegrationError(f"SUSFS copy changed after patching: {destination}")
    return sorted(verified)


def integrate(source_dir: Path, susfs_dir: Path, base: str) -> dict[str, Any]:
    if base not in BASES:
        raise IntegrationError(f"unsupported OnePlus base {base!r}")
    source = _require_plain_directory(source_dir, "kernel tree")
    susfs = _require_plain_directory(susfs_dir, "SUSFS")
    stamp = source / STAMP_NAME
    if stamp.exists() or stamp.is_symlink():
        raise IntegrationError(f"SUSFS kernel integration stamp already exists: {stamp}")
    existing_residue = _residue(source)
    if existing_residue:
        relative = [path.relative_to(source).as_posix() for path in existing_residue]
        raise IntegrationError(f"kernel tree contains patch residue: {relative}")

    version = _kernel_version(source)
    patch_file = _require_plain_file(susfs, PATCH_RELATIVE, "SUSFS Android 15 / 6.6 patch")
    target_relatives = _patch_targets(patch_file)
    target_files = [
        _require_plain_file(source, relative, f"patch target {relative.as_posix()}")
        for relative in target_relatives
    ]
    # GNU patch is intentionally used only with LF inputs.  This avoids its
    # platform-dependent CRLF backup behavior and makes residue checks stable.
    for relative, path in zip(target_relatives, target_files, strict=True):
        _read_lf_text(path, f"patch target {relative.as_posix()}")

    susfs_c = _require_plain_file(susfs, SUSFS_C_RELATIVE, "SUSFS implementation")
    header_directory = _assert_plain_components(susfs, HEADER_DIRECTORY_RELATIVE, "SUSFS header directory")
    if not header_directory.is_dir():
        raise IntegrationError(f"SUSFS header directory is not a directory: {header_directory}")
    header_files = sorted(header_directory.glob("susfs*.h"), key=lambda path: path.name)
    if not header_files:
        raise IntegrationError("SUSFS dependency contains no susfs*.h headers")
    for header in header_files:
        relative = header.relative_to(susfs)
        _require_plain_file(susfs, relative, f"SUSFS header {header.name}")
    copy_plan: list[tuple[Path, Path, Path, Path]] = [
        (susfs_c, _destination(source, Path("fs/susfs.c"), "SUSFS implementation destination"), SUSFS_C_RELATIVE, Path("fs/susfs.c"))
    ]
    for header in header_files:
        destination_relative = Path("include/linux") / header.name
        copy_plan.append(
            (
                header,
                _destination(source, destination_relative, f"SUSFS header destination {header.name}"),
                header.relative_to(susfs),
                destination_relative,
            )
        )

    namespace = _require_plain_file(source, Path("fs/namespace.c"), "namespace compatibility target")
    proc_base = _require_plain_file(source, Path("fs/proc/base.c"), "proc base compatibility target")
    memory = _require_plain_file(source, Path("mm/memory.c"), "memory compatibility target")
    task_mmu = _require_plain_file(source, Path("fs/proc/task_mmu.c"), "task_mmu compatibility target")
    compatibility_targets = {namespace, proc_base, memory, task_mmu}
    untracked_compatibility = compatibility_targets - set(target_files)
    if untracked_compatibility:
        relative = sorted(path.relative_to(source).as_posix() for path in untracked_compatibility)
        raise IntegrationError(f"compatibility edits are absent from the audited patch target set: {relative}")
    namespace_text, trace_inserted = _ensure_line_after(
        _read_lf_text(namespace, "namespace compatibility target"), TRACE_FS, TRACE_BLK, "fs/namespace.c"
    )
    proc_base_text, dma_inserted = _ensure_line_after(
        _read_lf_text(proc_base, "proc base compatibility target"), DMA_BUF, CPUFREQ_TIMES, "fs/proc/base.c"
    )
    memory_text, zswap_inserted = _ensure_line_after(
        _read_lf_text(memory, "memory compatibility target"), ZSWAP, SCHED_SYSCTL, "mm/memory.c"
    )
    task_text, task_inserted, last_vma_expanded = _prepare_task_mmu(
        _read_lf_text(task_mmu, "task_mmu compatibility target")
    )
    patch_tool = _gnu_patch_version()

    snapshots = {path: path.read_bytes() for path in target_files}
    created: list[Path] = []
    try:
        for source_file, destination, _, _ in copy_plan:
            created.append(destination)
            _atomic_copy(source_file, destination)
        if trace_inserted:
            _write_lf_text(namespace, namespace_text)
        if dma_inserted:
            _write_lf_text(proc_base, proc_base_text)
        if zswap_inserted:
            _write_lf_text(memory, memory_text)
        if task_inserted or last_vma_expanded:
            _write_lf_text(task_mmu, task_text)

        return_code, patch_output = _run_patch(source, patch_file)
        current_task = _read_lf_text(task_mmu, "patched task_mmu")
        restored_task = _restore_task_mmu(
            current_task,
            task_inserted=task_inserted,
            last_vma_expanded=last_vma_expanded,
        )
        if restored_task != current_task:
            _write_lf_text(task_mmu, restored_task)
        if return_code != 0:
            raise IntegrationError(
                f"SUSFS kernel patch failed with exit {return_code}\n{patch_output[-4000:]}"
            )
        residue = _residue(source)
        if residue:
            relative = [path.relative_to(source).as_posix() for path in residue]
            raise IntegrationError(f"SUSFS patch left residue: {relative}")

        copied_pairs = [(item[0], item[1]) for item in copy_plan]
        symbols = _assert_symbols(source, copied_pairs)
        outputs = [
            {"path": relative.as_posix(), "sha256": sha256_file(path)}
            for relative, path in sorted(
                zip(target_relatives, target_files, strict=True), key=lambda item: item[0].as_posix()
            )
        ]
        copied_records = [
            {
                "source": source_relative.as_posix(),
                "destination": destination_relative.as_posix(),
                "sha256": sha256_file(destination),
            }
            for _, destination, source_relative, destination_relative in copy_plan
        ]
        document: dict[str, Any] = {
            "schema_version": 1,
            "integration": "susfs-kernel-android15-6.6",
            "base": base,
            "kernel_version": version,
            "patch_tool": patch_tool,
            "patch": {
                "path": PATCH_RELATIVE.as_posix(),
                "sha256": sha256_file(patch_file),
                "strip": 1,
                "forward_only": True,
            },
            "compatibility": {
                "trace_hooks_fs_inserted": trace_inserted,
                "dma_buf_inserted": dma_inserted,
                "zswap_inserted": zswap_inserted,
                "task_mmu_declarations_temporary": task_inserted,
                "last_vma_end_expansion_temporary": last_vma_expanded,
            },
            "copied_files": copied_records,
            "patched_files": outputs,
            "verified_symbols": symbols,
        }
        _atomic_json(stamp, document)
        created.append(stamp)
        return document
    except BaseException:
        # A failed integration must be retryable from the exact input tree.
        for path, contents in snapshots.items():
            path.write_bytes(contents)
        for path in reversed(created):
            if path.is_file() or path.is_symlink():
                path.unlink()
        for path in _residue(source):
            path.unlink()
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--susfs-dir", required=True, type=Path)
    parser.add_argument("--base", required=True, choices=tuple(sorted(BASES)))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        document = integrate(args.source_dir, args.susfs_dir, args.base)
    except IntegrationError as exc:
        print(f"integration error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(document, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
