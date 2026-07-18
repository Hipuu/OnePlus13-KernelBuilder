#!/usr/bin/env python3
"""Make the pinned HMBIRD device-tree converter executable.

The locked WildKernels overwriter patch creates ``convert_configs.sh`` as
mode 0644 but invokes it directly from Kbuild.  Verify the exact dependency
commit, patch blob, and generated script in both kernel trees before changing
only the script modes to 0755 as one rollback-safe transaction.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


TARGET_RELATIVE = Path(
    "drivers/of/overwriter/overwrite_configs/convert_configs.sh"
)
STAMP_RELATIVE = Path(".op13/hmbird-overwriter-mode.json")
TREE_RELATIVES = MappingProxyType(
    {
        "common": Path("kernel_platform/common"),
        "msm-kernel": Path("kernel_platform/msm-kernel"),
    }
)


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def git_blob_oid(payload: bytes) -> str:
    header = f"blob {len(payload)}\0".encode("ascii")
    return hashlib.sha1(header + payload, usedforsecurity=False).hexdigest()


@dataclass(frozen=True)
class ModeContract:
    bases: tuple[str, ...]
    wild_commit: str
    patch_path: str
    patch_blob: str
    patch_sha256: str
    patch_size: int
    target_blob: str
    target_sha256: str
    target_size: int
    pre_mode: int
    post_mode: int


CONTRACT = ModeContract(
    bases=("oos15-cn", "oos15-global", "oos16"),
    wild_commit="2ee34500cb4c3ee954ba36090e11f6ff08b3ec2f",
    patch_path="oneplus/hmbird/overwriter.patch",
    patch_blob="7a573dbe50eecaa2ca89b325dce3b274a2d4bd91",
    patch_sha256="f9963385662591cab6c7ca159628c83a50ba7cf834a726e7749880046a6c8572",
    patch_size=39515,
    target_blob="b05dabb860650cc721702f63fc22f093de621958",
    target_sha256="cf3077459d8b1023912a3eac9996d9133503b192d080de186a59663d4b050418",
    target_size=3528,
    pre_mode=0o644,
    post_mode=0o755,
)


class IntegrationError(RuntimeError):
    """The HMBIRD overwriter input violates its locked mode contract."""


def _is_link_like(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    except FileNotFoundError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _assert_plain_components(path: Path, label: str) -> Path:
    absolute = _absolute(path)
    cursor = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        cursor /= part
        if not cursor.exists():
            raise IntegrationError(f"{label} is missing: {cursor}")
        if _is_link_like(cursor):
            raise IntegrationError(
                f"{label} contains a symlink or reparse point: {cursor}"
            )
    return absolute


def _require_plain_directory(path: Path, label: str) -> Path:
    result = _assert_plain_components(path, label)
    if not result.is_dir():
        raise IntegrationError(f"{label} is not a directory: {result}")
    return result


def _require_plain_file(path: Path, label: str) -> Path:
    result = _assert_plain_components(path, label)
    if not result.is_file():
        raise IntegrationError(f"{label} is not a regular file: {result}")
    return result


def _run_git(checkout: Path, *arguments: str, text: bool = True) -> str | bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(checkout), *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=text,
            check=False,
        )
    except FileNotFoundError as exc:
        raise IntegrationError("Git is required to verify the Wild patch") from exc
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if isinstance(stderr, bytes):
            detail = stderr.decode("utf-8", errors="replace")
        else:
            detail = stderr
        raise IntegrationError(f"Wild patch Git verification failed: {detail}")
    return result.stdout


def _verify_wild_patch(wild: Path, contract: ModeContract) -> dict[str, str | int]:
    head = str(_run_git(wild, "rev-parse", "HEAD")).strip()
    if head != contract.wild_commit:
        raise IntegrationError(
            f"Wild patch commit changed: expected {contract.wild_commit}, got {head}"
        )
    status = str(_run_git(wild, "status", "--porcelain", "--untracked-files=all"))
    if status:
        raise IntegrationError("Wild patch checkout is not byte-clean")
    payload = bytes(
        _run_git(
            wild,
            "show",
            f"HEAD:{contract.patch_path}",
            text=False,
        )
    )
    digest = sha256_bytes(payload)
    blob = git_blob_oid(payload)
    if (
        len(payload) != contract.patch_size
        or digest != contract.patch_sha256
        or blob != contract.patch_blob
    ):
        raise IntegrationError(
            "Wild HMBIRD overwriter patch changed: "
            f"expected {contract.patch_sha256}, got {digest}"
        )
    return {
        "path": contract.patch_path,
        "blob": blob,
        "sha256": digest,
        "size": len(payload),
    }


def _read_target(path: Path, contract: ModeContract) -> tuple[bytes, int]:
    payload = path.read_bytes()
    if b"\x00" in payload or b"\r" in payload or not payload.endswith(b"\n"):
        raise IntegrationError(f"HMBIRD converter is not LF-only text: {path}")
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise IntegrationError(f"HMBIRD converter is not UTF-8: {path}") from exc
    digest = sha256_bytes(payload)
    if (
        len(payload) != contract.target_size
        or digest != contract.target_sha256
        or git_blob_oid(payload) != contract.target_blob
    ):
        raise IntegrationError(
            "HMBIRD converter preimage changed: "
            f"expected {contract.target_sha256}, got {digest}"
        )
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode == contract.post_mode:
        raise IntegrationError(f"HMBIRD converter is already executable: {path}")
    if mode != contract.pre_mode:
        raise IntegrationError(
            f"HMBIRD converter mode changed: expected {contract.pre_mode:04o}, "
            f"got {mode:04o}"
        )
    return payload, mode


def _set_mode(path: Path, mode: int) -> None:
    path.chmod(mode)


def _atomic_bytes(path: Path, payload: bytes, mode: int) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _atomic_bytes(path, payload, 0o644)


def integrate(
    source_dir: Path,
    wild_dir: Path,
    base: str,
    *,
    contract: ModeContract = CONTRACT,
) -> dict[str, Any]:
    if base not in contract.bases:
        raise IntegrationError(
            f"overwriter mode repair is not valid for base {base!r}"
        )
    source = _require_plain_directory(source_dir, "locked source checkout")
    wild = _require_plain_directory(wild_dir, "Wild patch checkout")
    metadata = _require_plain_directory(source / STAMP_RELATIVE.parent, "builder metadata")
    stamp = metadata / STAMP_RELATIVE.name
    if stamp.exists() or stamp.is_symlink():
        raise IntegrationError(f"overwriter mode stamp already exists: {stamp}")
    patch_record = _verify_wild_patch(wild, contract)

    targets: dict[str, Path] = {}
    snapshots: dict[str, tuple[bytes, int]] = {}
    for tree, relative in TREE_RELATIVES.items():
        target = _require_plain_file(
            source / relative / TARGET_RELATIVE,
            f"{tree} HMBIRD converter",
        )
        targets[tree] = target
        snapshots[tree] = _read_target(target, contract)

    try:
        for tree in sorted(targets):
            target = targets[tree]
            before, _ = snapshots[tree]
            _set_mode(target, contract.post_mode)
            if target.read_bytes() != before:
                raise IntegrationError(f"{tree} HMBIRD converter content changed")
            mode = stat.S_IMODE(target.stat().st_mode)
            if mode != contract.post_mode:
                raise IntegrationError(
                    f"{tree} HMBIRD converter mode repair failed: got {mode:04o}"
                )

        document: dict[str, Any] = {
            "schema_version": 1,
            "integration": "oneplus-hmbird-overwriter-executable",
            "base": base,
            "inputs": {
                "wild_commit": contract.wild_commit,
                "patch": patch_record,
            },
            "targets": {
                tree: {
                    "path": (TREE_RELATIVES[tree] / TARGET_RELATIVE).as_posix(),
                    "blob": contract.target_blob,
                    "sha256": contract.target_sha256,
                    "size": contract.target_size,
                    "pre_mode": f"{contract.pre_mode:04o}",
                    "post_mode": f"{contract.post_mode:04o}",
                }
                for tree in sorted(targets)
            },
            "repair": "restore direct Kbuild executability without changing bytes",
        }
        _atomic_json(stamp, document)
        return document
    except BaseException:
        for tree, target in targets.items():
            before, mode = snapshots[tree]
            if target.is_file():
                if target.read_bytes() != before:
                    _atomic_bytes(target, before, mode)
                elif stat.S_IMODE(target.stat().st_mode) != mode:
                    _set_mode(target, mode)
        stamp.unlink(missing_ok=True)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--wild-dir", required=True, type=Path)
    parser.add_argument("--base", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        document = integrate(args.source_dir, args.wild_dir, args.base)
    except IntegrationError as exc:
        print(f"integration error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(document, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
