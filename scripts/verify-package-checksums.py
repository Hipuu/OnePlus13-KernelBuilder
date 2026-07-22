#!/usr/bin/env python3
"""Verify the sole packaged SHA256SUMS writer and its sealed build context."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any


CHECKSUM_NAME = "SHA256SUMS"
LINE_RE = re.compile(r"([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._+-]{0,254})\Z")


class VerificationError(RuntimeError):
    """The packaged checksum or context contract changed."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise VerificationError(f"cannot hash package file {path.name}: {exc}") from exc
    return digest.hexdigest()


def _plain_directory(path: Path) -> Path:
    raw = Path(path)
    if raw.is_symlink():
        raise VerificationError(f"package directory must not be a symlink: {raw}")
    try:
        resolved = raw.resolve(strict=True)
    except OSError as exc:
        raise VerificationError(f"package directory is missing: {raw}") from exc
    if not resolved.is_dir():
        raise VerificationError(f"package path is not a directory: {resolved}")
    return resolved


def _plain_file(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise VerificationError(f"{label} must be a plain file: {path}")
    return path


def _read_context(path: Path) -> dict[str, Any]:
    _plain_file(path, "packaged build context")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VerificationError(f"packaged build context is invalid: {exc}") from exc
    if not isinstance(document, dict) or document.get("stage") != "packaged":
        raise VerificationError("build context has not reached the packaged stage")
    return document


def _record_name(record: dict[str, Any]) -> str:
    value = record.get("path")
    if not isinstance(value, str) or not value or "\x00" in value:
        raise VerificationError("package context contains an invalid path")
    normalized = value.replace("\\", "/")
    name = PurePosixPath(normalized).name
    if LINE_RE.fullmatch(f"{'0' * 64}  {name}") is None:
        raise VerificationError(f"package context contains an unsafe filename: {name!r}")
    return name


def verify(directory: Path, context_path: Path) -> dict[str, Any]:
    root = _plain_directory(directory)
    checksum_path = _plain_file(root / CHECKSUM_NAME, CHECKSUM_NAME)
    try:
        payload = checksum_path.read_bytes()
        text = payload.decode("ascii", "strict")
    except (OSError, UnicodeError) as exc:
        raise VerificationError(f"{CHECKSUM_NAME} must be ASCII: {exc}") from exc
    if not payload or b"\r" in payload or not payload.endswith(b"\n"):
        raise VerificationError(f"{CHECKSUM_NAME} must be non-empty LF-terminated text")

    listed: dict[str, str] = {}
    lines = text.splitlines()
    for line in lines:
        match = LINE_RE.fullmatch(line)
        if match is None:
            raise VerificationError(f"invalid {CHECKSUM_NAME} line: {line!r}")
        digest, name = match.groups()
        if name == CHECKSUM_NAME or name in listed:
            raise VerificationError(f"duplicate or recursive checksum entry: {name}")
        listed[name] = digest
    if list(listed) != sorted(listed):
        raise VerificationError(f"{CHECKSUM_NAME} entries are not canonically sorted")

    actual: dict[str, Path] = {}
    try:
        children = sorted(root.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise VerificationError(f"cannot enumerate package directory: {exc}") from exc
    for child in children:
        if child.is_symlink() or not child.is_file():
            raise VerificationError(f"package output contains a non-regular member: {child.name}")
        actual[child.name] = child
    expected_names = sorted(name for name in actual if name != CHECKSUM_NAME)
    if sorted(listed) != expected_names:
        missing = sorted(set(expected_names) - set(listed))
        extra = sorted(set(listed) - set(expected_names))
        raise VerificationError(
            f"{CHECKSUM_NAME} coverage differs; missing={missing}, extra={extra}"
        )
    observed = {name: _sha256(actual[name]) for name in expected_names}
    for name, digest in listed.items():
        if observed[name] != digest:
            raise VerificationError(f"checksum mismatch: {name}")

    context = _read_context(Path(context_path))
    packages = context.get("packages")
    if not isinstance(packages, list) or not packages:
        raise VerificationError("packaged build context has no package records")
    context_records: dict[str, dict[str, Any]] = {}
    for index, value in enumerate(packages):
        if not isinstance(value, dict):
            raise VerificationError(f"package context record {index} is invalid")
        name = _record_name(value)
        if name in context_records:
            raise VerificationError(f"package context repeats a filename: {name}")
        digest = value.get("sha256")
        size = value.get("size")
        if (
            not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
        ):
            raise VerificationError(f"package context record metadata is invalid: {name}")
        context_records[name] = value
    if sorted(context_records) != sorted(actual):
        missing = sorted(set(actual) - set(context_records))
        extra = sorted(set(context_records) - set(actual))
        raise VerificationError(
            f"package context coverage differs; missing={missing}, extra={extra}"
        )
    for name, path in actual.items():
        record = context_records[name]
        if record["size"] != path.stat().st_size or record["sha256"] != _sha256(path):
            raise VerificationError(f"package context digest differs: {name}")
    if context_records[CHECKSUM_NAME].get("role") != "checksums":
        raise VerificationError("package context does not identify the checksum record")

    return {
        "schema_version": 1,
        "status": "verified",
        "file_count": len(actual),
        "checksummed_file_count": len(listed),
        "sha256sums_sha256": _sha256(checksum_path),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--context", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = verify(args.directory, args.context)
    except VerificationError as exc:
        print(f"package checksum verification: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
