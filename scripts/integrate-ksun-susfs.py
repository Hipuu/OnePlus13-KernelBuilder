#!/usr/bin/env python3
"""Integrate SUSFS v2.2.0 into the pinned KernelSU-Next v3.3.0 tree.

This helper intentionally models the audited, partially-rejecting upstream
patch sequence.  Any change to that reject fingerprint, patch order, or final
Kconfig surface stops the build instead of guessing at a merge.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


EXPECTED_REJECTS = {
    "kernel/Kbuild.rej",
    "kernel/core/init.c.rej",
    "kernel/feature/kernel_umount.c.rej",
    "kernel/feature/sucompat.c.rej",
    "kernel/hook/setuid_hook.c.rej",
    "kernel/supercall/supercall.c.rej",
}
FIX_PATCHES = (
    "fix_Kbuild.patch",
    "fix_init.c.patch",
    "fix_kernel_umount.c.patch",
    "fix_sucompat.c.patch",
    "fix_setuid_hook.c.patch",
    "fix_supercall.c.patch",
    "ksu_toolkit.patch",
)
SUSFS_PATCH = Path("kernel_patches/KernelSU/10_enable_susfs_for_ksu.patch")
WILD_FIX_DIR = Path("next/susfs_fix_patches/v2.2.0")
EXPECTED_SUSFS_SHA256 = "1be114c12dde6aa9f67b79db9eb88d19355af29aa1445e5e6361dad6f18d9a19"
EXPECTED_FIX_SHA256 = {
    "fix_Kbuild.patch": "e6ac82983b97ee0144c8c7b72572f48ec9556c14d353c223d26736e480d62b69",
    "fix_init.c.patch": "b8c743c0dc2d22729735bf20ead52f14a3c3b837f9670c5ce984f402cff4615b",
    "fix_kernel_umount.c.patch": "da466b9e6412a5ee066d4f40d73c338ab3acf3832cb34474e158395c6e4bf525",
    "fix_sucompat.c.patch": "518d70d30ddc8bd41bde9b4f8288a482dd2ff2dbc194b1958cb1f7cc0753a079",
    "fix_setuid_hook.c.patch": "d0cfa90ecba9dd99ce5517ae63387d0f7156523850c285d87c9ec23e6666f988",
    "fix_supercall.c.patch": "378640c04d5bf9271d6b294d0efdd0040568615ae16f1cb696455343fd5ebc17",
    "ksu_toolkit.patch": "a79b2a3f1bd0e314765694da29252039df7d6fd5e72423007505798b8ce115af",
}


class IntegrationError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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


def _require_directory(path: Path, label: str) -> Path:
    resolved = path.resolve()
    if not resolved.is_dir():
        raise IntegrationError(f"{label} directory is missing: {resolved}")
    return resolved


def _require_file(path: Path, label: str) -> Path:
    resolved = path.resolve()
    if not resolved.is_file():
        raise IntegrationError(f"{label} file is missing: {resolved}")
    return resolved


def _residue(root: Path, suffix: str) -> set[str]:
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob(f"*{suffix}")
        if path.is_file()
    }


def _run_patch(tree: Path, patch_file: Path) -> tuple[int, str]:
    command = [
        "patch",
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


def _git_blob_bytes(checkout: Path, relative: Path, label: str) -> bytes:
    if not (checkout / ".git").exists():
        raise IntegrationError(f"{label} checkout lacks Git metadata: {checkout}")
    result = subprocess.run(
        ["git", "-C", str(checkout), "show", f"HEAD:{relative.as_posix()}"],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise IntegrationError(f"failed to read pinned {label} blob: {detail}")
    return result.stdout


def _run_patch_payload(tree: Path, payload: bytes, filename: str) -> tuple[int, str]:
    with tempfile.TemporaryDirectory(prefix="op13-ksun-patch-") as temporary_name:
        patch_file = Path(temporary_name) / filename
        patch_file.write_bytes(payload)
        return _run_patch(tree, patch_file)


def _gnu_patch_version() -> str:
    try:
        result = subprocess.run(
            ["patch", "--version"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
    except FileNotFoundError as exc:
        raise IntegrationError("GNU patch is required") from exc
    first_line = result.stdout.splitlines()[0] if result.stdout else ""
    if result.returncode != 0 or "GNU patch" not in first_line:
        raise IntegrationError(f"unsupported patch implementation: {first_line!r}")
    return first_line


def integrate(ksun_dir: Path, susfs_dir: Path, wild_dir: Path, stamp: Path | None = None) -> dict[str, Any]:
    ksun = _require_directory(ksun_dir, "KernelSU-Next")
    susfs = _require_directory(susfs_dir, "SUSFS")
    wild = _require_directory(wild_dir, "Wild patch")
    stamp_path = (stamp.resolve() if stamp is not None else ksun / ".op13-susfs-integrated.json")
    try:
        stamp_path.relative_to(ksun)
    except ValueError as exc:
        raise IntegrationError("integration stamp must stay inside the KernelSU-Next tree") from exc
    if stamp_path.exists():
        raise IntegrationError(f"integration stamp already exists: {stamp_path}")
    initial_rejects = _residue(ksun, ".rej")
    initial_originals = _residue(ksun, ".orig")
    if initial_rejects or initial_originals:
        raise IntegrationError(
            f"KernelSU-Next tree is not clean (rejects={sorted(initial_rejects)}, originals={sorted(initial_originals)})"
        )
    version = _gnu_patch_version()
    susfs_patch = _require_file(susfs / SUSFS_PATCH, "SUSFS integration patch")
    susfs_payload = _git_blob_bytes(susfs, SUSFS_PATCH, "SUSFS")
    susfs_sha256 = hashlib.sha256(susfs_payload).hexdigest()
    if susfs_sha256 != EXPECTED_SUSFS_SHA256:
        raise IntegrationError(
            f"SUSFS integration patch changed: expected {EXPECTED_SUSFS_SHA256}, got {susfs_sha256}"
        )
    base_return, base_output = _run_patch_payload(ksun, susfs_payload, SUSFS_PATCH.name)
    if base_return != 1:
        raise IntegrationError(
            f"SUSFS base patch exit fingerprint changed: expected 1, got {base_return}\n{base_output[-4000:]}"
        )
    actual_rejects = _residue(ksun, ".rej")
    if actual_rejects != EXPECTED_REJECTS:
        raise IntegrationError(
            "SUSFS reject fingerprint changed: "
            f"expected={sorted(EXPECTED_REJECTS)}, actual={sorted(actual_rejects)}"
        )
    fix_records: list[dict[str, Any]] = []
    fix_root = _require_directory(wild / WILD_FIX_DIR, "Wild v2.2.0 fix")
    for filename in FIX_PATCHES:
        patch_file = _require_file(fix_root / filename, f"Wild fix {filename}")
        patch_payload = _git_blob_bytes(wild, WILD_FIX_DIR / filename, f"Wild fix {filename}")
        patch_sha256 = hashlib.sha256(patch_payload).hexdigest()
        if patch_sha256 != EXPECTED_FIX_SHA256[filename]:
            raise IntegrationError(
                f"Wild fix {filename} changed: expected {EXPECTED_FIX_SHA256[filename]}, "
                f"got {patch_sha256}"
            )
        return_code, output = _run_patch_payload(ksun, patch_payload, filename)
        if return_code != 0:
            raise IntegrationError(
                f"Wild fix {filename} failed with exit {return_code}\n{output[-4000:]}"
            )
        fix_records.append(
            {
                "name": filename,
                "path": str(patch_file),
                "sha256": patch_sha256,
                "exit_code": return_code,
            }
        )
    # Only the exact, already-verified reject set is removed.
    for relative in sorted(EXPECTED_REJECTS):
        reject = (ksun / relative).resolve()
        try:
            reject.relative_to(ksun)
        except ValueError as exc:
            raise IntegrationError(f"reject path escaped KernelSU-Next tree: {relative}") from exc
        if not reject.is_file():
            raise IntegrationError(f"verified reject disappeared before cleanup: {relative}")
        reject.unlink()
    remaining_rejects = _residue(ksun, ".rej")
    remaining_originals = _residue(ksun, ".orig")
    if remaining_rejects or remaining_originals:
        raise IntegrationError(
            f"patch residue remains (rejects={sorted(remaining_rejects)}, originals={sorted(remaining_originals)})"
        )
    kconfig = _require_file(ksun / "kernel" / "Kconfig", "KernelSU-Next Kconfig")
    kconfig_text = kconfig.read_text(encoding="utf-8")
    if "CONFIG_KSU_SUSFS" not in kconfig_text and "KSU_SUSFS" not in kconfig_text:
        raise IntegrationError("CONFIG_KSU_SUSFS is absent after integration")
    document: dict[str, Any] = {
        "schema_version": 1,
        "integration": "kernelsu-next-susfs-v2.2.0",
        "patch_tool": version,
        "base_patch": {
            "path": str(susfs_patch),
            "sha256": susfs_sha256,
            "expected_exit_code": 1,
            "rejects": sorted(EXPECTED_REJECTS),
        },
        "fix_patches": fix_records,
        "kconfig": {"path": str(kconfig), "sha256": sha256_file(kconfig)},
    }
    _atomic_json(stamp_path, document)
    return document


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ksun-dir", required=True, type=Path)
    parser.add_argument("--susfs-dir", required=True, type=Path)
    parser.add_argument("--wild-dir", required=True, type=Path)
    parser.add_argument("--stamp", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        document = integrate(args.ksun_dir, args.susfs_dir, args.wild_dir, args.stamp)
    except IntegrationError as exc:
        print(f"integration error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(document, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
