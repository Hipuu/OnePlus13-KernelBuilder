#!/usr/bin/env python3
"""Install an audited KernelSU kernel subtree into the common driver tree."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any


VARIANTS = {"kernelsu", "kernelsu-next"}
EXCLUDED_PARTS = {".git", ".cache", "__pycache__"}
STAMP_NAME = ".op13-root-source.json"


class InstallError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _safe_path(raw: Path, workspace: Path, label: str) -> Path:
    if not raw.is_absolute() or ".." in raw.parts:
        raise InstallError(f"{label} must be an absolute path without parent traversal")
    resolved = raw.resolve()
    if not _inside(resolved, workspace) or resolved == workspace:
        raise InstallError(f"{label} escapes the source workspace")
    return resolved


def _source_files(source: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(source.rglob("*"), key=lambda item: item.relative_to(source).as_posix()):
        relative = path.relative_to(source)
        if any(part in EXCLUDED_PARTS for part in relative.parts) or path.name.endswith(".pyc"):
            continue
        if path.is_symlink():
            raise InstallError(f"root driver source contains a symlink: {relative.as_posix()}")
        if path.is_file():
            files.append(path)
    return files


def tree_digest(source: Path, files: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(source).as_posix().encode("utf-8")
        mode = b"755" if path.stat().st_mode & stat.S_IXUSR else b"644"
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(mode)
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    return digest.hexdigest()


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


def install(workspace: Path, root_dir: Path, destination: Path, variant: str) -> dict[str, Any]:
    if variant not in VARIANTS:
        raise InstallError(f"unsupported root variant {variant!r}")
    workspace_resolved = workspace.resolve()
    if not workspace_resolved.is_dir():
        raise InstallError(f"workspace is missing: {workspace_resolved}")
    root_resolved = _safe_path(root_dir, workspace_resolved, "root directory")
    destination_resolved = _safe_path(destination, workspace_resolved, "destination")
    if not root_resolved.is_dir():
        raise InstallError(f"root directory is missing: {root_resolved}")
    if _inside(destination_resolved, root_resolved) or _inside(root_resolved, destination_resolved):
        raise InstallError("root directory and destination must not contain each other")
    if destination_resolved.exists():
        raise InstallError(f"root driver destination already exists: {destination_resolved}")
    source = root_resolved / "kernel"
    if not source.is_dir():
        raise InstallError(f"root kernel subtree is missing: {source}")
    required = (source / "Kconfig", source / "Kbuild")
    for path in required:
        if not path.is_file() or path.is_symlink():
            raise InstallError(f"required root driver file is missing or unsafe: {path}")
    files = _source_files(source)
    if not files:
        raise InstallError("root kernel subtree has no files")
    tree_sha256 = tree_digest(source, files)
    temporary = destination_resolved.parent / f".{destination_resolved.name}.tmp"
    if temporary.exists():
        raise InstallError(f"stale root driver temporary directory: {temporary}")
    destination_resolved.parent.mkdir(parents=True, exist_ok=True)
    try:
        temporary.mkdir()
        for path in files:
            relative = path.relative_to(source)
            target = temporary / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        copied_files = _source_files(temporary)
        copied_sha256 = tree_digest(temporary, copied_files)
        if copied_sha256 != tree_sha256:
            raise InstallError("copied root driver tree digest mismatch")
        document: dict[str, Any] = {
            "schema_version": 1,
            "variant": variant,
            "tree_sha256": tree_sha256,
            "file_count": len(files),
            "kconfig_sha256": sha256_file(source / "Kconfig"),
            "kbuild_sha256": sha256_file(source / "Kbuild"),
        }
        _atomic_json(temporary / STAMP_NAME, document)
        temporary.replace(destination_resolved)
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return document


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--root-dir", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--variant", required=True, choices=tuple(sorted(VARIANTS)))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        document = install(args.workspace, args.root_dir, args.destination, args.variant)
    except InstallError as exc:
        print(f"install error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(document, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
