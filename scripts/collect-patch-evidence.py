#!/usr/bin/env python3
"""Copy bounded patch reject/backup files into the durable debug artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any


RESIDUE_SUFFIXES = (".orig", ".rej")
MAX_RESIDUE_FILES = 256
MAX_MEMBER_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
SAFE_COMPONENT = re.compile(r"[A-Za-z0-9._+@-]+\Z")
WINDOWS_REPARSE_ATTRIBUTE = 0x400


class CollectionError(RuntimeError):
    """Patch evidence could not be collected without weakening path safety."""


def _lexists(path: Path) -> bool:
    return os.path.lexists(os.fspath(path))


def _is_windows_reparse(metadata: os.stat_result) -> bool:
    return bool(
        getattr(metadata, "st_reparse_tag", 0)
        or (
            getattr(metadata, "st_file_attributes", 0)
            & WINDOWS_REPARSE_ATTRIBUTE
        )
    )


def _is_junction(path: Path) -> bool:
    predicate = getattr(path, "is_junction", None)
    if predicate is None:
        return False
    try:
        return bool(predicate())
    except OSError as exc:
        raise CollectionError(f"cannot inspect possible junction {path}: {exc}") from exc


def _reject_reparse(path: Path, metadata: os.stat_result, label: str) -> None:
    if _is_windows_reparse(metadata) or _is_junction(path):
        raise CollectionError(f"{label} must not be a junction or reparse point: {path}")


def _path_chain(path: Path) -> list[Path]:
    current = Path(os.path.abspath(os.fspath(path)))
    chain: list[Path] = []
    while True:
        chain.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent
    return list(reversed(chain))


def _require_plain_path_chain(path: Path, label: str) -> None:
    for component in _path_chain(path):
        if not _lexists(component):
            raise CollectionError(f"{label} path component is missing: {component}")
        metadata = component.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise CollectionError(f"{label} must not traverse a symlink: {component}")
        _reject_reparse(component, metadata, label)


def _plain_directory(path: Path, label: str, *, create: bool = False) -> Path:
    raw = Path(path)
    if create and not _lexists(raw):
        _require_plain_path_chain(raw.parent, f"{label} parent")
        try:
            raw.mkdir()
        except OSError as exc:
            raise CollectionError(f"cannot create {label}: {raw}: {exc}") from exc
    _require_plain_path_chain(raw, label)
    try:
        resolved = raw.resolve(strict=True)
    except OSError as exc:
        raise CollectionError(f"{label} is missing: {raw}") from exc
    if not resolved.is_dir():
        raise CollectionError(f"{label} must be a directory: {resolved}")
    return resolved


def _safe_relative(path: Path, source_root: Path) -> str:
    try:
        relative = path.relative_to(source_root)
    except ValueError as exc:
        raise CollectionError(f"patch residue escaped the source tree: {path}") from exc
    if (
        not relative.parts
        or len(relative.parts) > 64
        or any(
            part in {"", ".", ".."} or SAFE_COMPONENT.fullmatch(part) is None
            for part in relative.parts
        )
    ):
        raise CollectionError(f"patch residue has an unsafe path: {relative}")
    return relative.as_posix()


def _walk_residues(source_root: Path) -> list[Path]:
    matches: list[Path] = []

    def fail_walk(exc: OSError) -> None:
        raise CollectionError(f"cannot enumerate source tree: {exc}") from exc

    for current, directories, filenames in os.walk(
        source_root,
        topdown=True,
        onerror=fail_walk,
        followlinks=False,
    ):
        current_path = Path(current)
        retained_directories: list[str] = []
        for name in sorted(directories):
            child = current_path / name
            metadata = child.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                continue
            _reject_reparse(child, metadata, "source directory")
            retained_directories.append(name)
        directories[:] = retained_directories
        for name in sorted(filenames):
            if not name.endswith(RESIDUE_SUFFIXES):
                continue
            candidate = current_path / name
            metadata = candidate.lstat()
            _reject_reparse(candidate, metadata, "patch residue")
            if not stat.S_ISREG(metadata.st_mode):
                raise CollectionError(
                    f"patch residue must be a regular file: "
                    f"{_safe_relative(candidate, source_root)}"
                )
            if metadata.st_nlink != 1:
                raise CollectionError(
                    f"hard-linked patch residue is forbidden: "
                    f"{_safe_relative(candidate, source_root)}"
                )
            matches.append(candidate)
            if len(matches) > MAX_RESIDUE_FILES:
                raise CollectionError(
                    f"patch residue count exceeds {MAX_RESIDUE_FILES} files"
                )
    return sorted(matches, key=lambda path: _safe_relative(path, source_root))


def _read_regular_file(path: Path, relative: str) -> tuple[bytes, os.stat_result]:
    before = path.lstat()
    _reject_reparse(path, before, "patch residue")
    if not stat.S_ISREG(before.st_mode):
        raise CollectionError(f"patch residue must be a regular file: {relative}")
    if before.st_nlink != 1:
        raise CollectionError(f"hard-linked patch residue is forbidden: {relative}")
    if before.st_size > MAX_MEMBER_BYTES:
        raise CollectionError(
            f"patch residue exceeds {MAX_MEMBER_BYTES} bytes: {relative}"
        )
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CollectionError(f"cannot safely open patch residue: {relative}: {exc}") from exc
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (
                opened.st_dev,
                opened.st_ino,
                opened.st_nlink,
                opened.st_size,
                opened.st_mtime_ns,
            )
            != (
                before.st_dev,
                before.st_ino,
                before.st_nlink,
                before.st_size,
                before.st_mtime_ns,
            )
        ):
            raise CollectionError(f"patch residue changed while being opened: {relative}")
        payload = stream.read(MAX_MEMBER_BYTES + 1)
    if len(payload) != opened.st_size:
        raise CollectionError(f"patch residue changed while being read: {relative}")
    return payload, opened


def _write_exclusive(path: Path, payload: bytes) -> None:
    _require_plain_path_chain(path.parent, "patch evidence parent")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise CollectionError(f"cannot create patch evidence file {path}: {exc}") from exc
    with os.fdopen(descriptor, "wb") as stream:
        written = stream.write(payload)
        if written != len(payload):
            raise CollectionError(f"short write while creating patch evidence file: {path}")
        stream.flush()
        os.fsync(stream.fileno())


def _remove_staging_directory(path: Path, debug_root: Path) -> None:
    if not _lexists(path):
        return
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise CollectionError(f"patch evidence staging path changed type: {path}")
    _reject_reparse(path, metadata, "patch evidence staging directory")
    try:
        path.resolve(strict=True).relative_to(debug_root)
    except (OSError, ValueError) as exc:
        raise CollectionError("patch evidence staging path escaped debug output") from exc
    shutil.rmtree(path)


def collect(source_dir: Path, output_dir: Path) -> dict[str, Any]:
    source_root = _plain_directory(source_dir, "source directory")
    debug_root = _plain_directory(output_dir, "debug output directory", create=True)
    try:
        debug_root.relative_to(source_root)
    except ValueError:
        pass
    else:
        raise CollectionError("debug output directory must remain outside the source tree")

    evidence_root = debug_root / "patch-residue"
    if evidence_root.exists() or evidence_root.is_symlink():
        raise CollectionError(
            f"patch evidence destination already exists: {evidence_root}"
        )

    records: list[dict[str, Any]] = []
    captured: list[tuple[str, bytes]] = []
    total_bytes = 0
    for source_path in _walk_residues(source_root):
        relative = _safe_relative(source_path, source_root)
        payload, metadata = _read_regular_file(source_path, relative)
        size = metadata.st_size
        total_bytes += size
        if total_bytes > MAX_TOTAL_BYTES:
            raise CollectionError(
                f"patch residue total exceeds {MAX_TOTAL_BYTES} bytes"
            )
        digest = hashlib.sha256(payload).hexdigest()
        evidence_name = f"{len(records):04d}-{digest}{source_path.suffix}"
        captured.append((evidence_name, payload))
        records.append(
            {
                "source_path": relative,
                "evidence_path": f"files/{evidence_name}",
                "size": size,
                "sha256": digest,
            }
        )

    document = {
        "schema_version": 1,
        "status": "captured",
        "file_count": len(records),
        "total_bytes": total_bytes,
        "limits": {
            "max_files": MAX_RESIDUE_FILES,
            "max_member_bytes": MAX_MEMBER_BYTES,
            "max_total_bytes": MAX_TOTAL_BYTES,
        },
        "files": records,
    }
    staging_root = Path(
        tempfile.mkdtemp(prefix=".patch-residue.", dir=debug_root)
    )
    active_error: BaseException | None = None
    try:
        staging_metadata = staging_root.lstat()
        if not stat.S_ISDIR(staging_metadata.st_mode):
            raise CollectionError("patch evidence staging path is not a directory")
        _reject_reparse(staging_root, staging_metadata, "patch evidence staging directory")
        files_root = staging_root / "files"
        files_root.mkdir()
        files_metadata = files_root.lstat()
        if not stat.S_ISDIR(files_metadata.st_mode):
            raise CollectionError("patch evidence files destination is not a directory")
        _reject_reparse(files_root, files_metadata, "patch evidence files destination")
        for evidence_name, payload in captured:
            _write_exclusive(files_root / evidence_name, payload)
        manifest = staging_root / "PATCH-RESIDUE-MANIFEST.json"
        _write_exclusive(
            manifest,
            (json.dumps(document, indent=2, sort_keys=True) + "\n").encode(
                "utf-8"
            ),
        )
        if _lexists(evidence_root):
            raise CollectionError(
                f"patch evidence destination already exists: {evidence_root}"
            )
        try:
            staging_root.rename(evidence_root)
        except OSError as exc:
            raise CollectionError(
                f"cannot publish patch evidence destination: {evidence_root}: {exc}"
            ) from exc
    except BaseException as exc:
        active_error = exc
        raise
    finally:
        try:
            _remove_staging_directory(staging_root, debug_root)
        except Exception as cleanup_error:
            if active_error is None:
                raise
            active_error.add_note(
                f"patch evidence staging cleanup also failed: {cleanup_error}"
            )
    return document


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        document = collect(args.source_dir, args.output_dir)
    except (CollectionError, OSError) as exc:
        print(f"patch evidence collection: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(document, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
