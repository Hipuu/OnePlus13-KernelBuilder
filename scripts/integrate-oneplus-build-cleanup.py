#!/usr/bin/env python3
"""Harden the locked OnePlus Bazel wrapper's GKI-header cleanup.

The upstream wrapper temporarily replaces selected ``msm-kernel`` headers
with symlinks into ``common``.  Its original finally block restores every path
in ``files_gki_aarch64.txt``, which also discards unrelated vendor-tree source
changes.  This helper accepts only the exact wrapper preimages pinned by this
repository, applies one repository-owned patch, verifies the exact postimage,
and records the full-byte transition.
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
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


REPO_ROOT = Path(__file__).resolve().parents[1]
TARGET_RELATIVE = Path("build_with_bazel.py")
PATCH_RELATIVE = Path(
    "patches/oneplus13/0007-build-with-bazel-restore-only-gki-headers.patch"
)
PATCH_SHA256 = "332a71c97f4e01edce9477eeec484bcab5daab5a8718080267f0ae32409f31c0"
STAMP_NAME = ".op13-build-with-bazel-cleanup.json"

PROFILE_CONTRACTS: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {
        "oos15-cn": MappingProxyType(
            {
                "commit": "d09a875fd283664a4ad3a8722fb608356985dab1",
                "pre_sha256": "492b95292dd8c0d3b8561eb97deb31b612ae4ba4c5a60f0da754ebd385621b59",
                "post_sha256": "2e0611c2a56da02112c74f1f9fa9ee0d44baa35f0a6858035fa97cd5d9ee1568",
            }
        ),
        "oos15-global": MappingProxyType(
            {
                "commit": "59336d4db04efdc70e1c63d6a92f7e4d14efafa8",
                "pre_sha256": "492b95292dd8c0d3b8561eb97deb31b612ae4ba4c5a60f0da754ebd385621b59",
                "post_sha256": "2e0611c2a56da02112c74f1f9fa9ee0d44baa35f0a6858035fa97cd5d9ee1568",
            }
        ),
        "oos16": MappingProxyType(
            {
                "commit": "73ecb0dc41fb28ce5727465bd19d7469b4a6db73",
                "pre_sha256": "8642456bbd6ea5bdf678bb80d8076df30738c7e10811e8a6b240a4017aba3676",
                "post_sha256": "3fe60bcde6de22f72cde4b8d73dd3cae343d274b92572982f5f8b51393972327",
            }
        ),
    }
)

POSTIMAGE_MARKERS: Mapping[str, int] = MappingProxyType(
    {
        "gki_headers_to_restore = []": 1,
        "gki_headers_to_restore.append(f)": 1,
        '["git", "checkout", "--pathspec-from-file=-"]': 1,
        'f"{path}\\n" for path in gki_headers_to_restore': 1,
    }
)
REMOVED_MARKER = "--pathspec-from-file=files_gki_aarch64.txt"


class IntegrationError(RuntimeError):
    """The input did not match the locked OnePlus cleanup contract."""


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


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
            raise IntegrationError(f"{label} contains a symlink or reparse point: {cursor}")
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


def _read_source(path: Path) -> bytes:
    value = path.read_bytes()
    if b"\x00" in value:
        raise IntegrationError(f"OnePlus build wrapper contains a NUL byte: {path}")
    if b"\r" in value:
        raise IntegrationError(f"OnePlus build wrapper is not LF-only: {path}")
    try:
        value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise IntegrationError(f"OnePlus build wrapper is not UTF-8: {path}") from exc
    return value


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
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


def _git_version() -> str:
    try:
        result = subprocess.run(
            ["git", "--version"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
    except FileNotFoundError as exc:
        raise IntegrationError("Git is required to apply the OnePlus cleanup patch") from exc
    version = result.stdout.strip()
    if result.returncode != 0 or not version.startswith("git version "):
        raise IntegrationError(f"unsupported Git executable: {version!r}")
    return version


def _run_git_apply(source: Path, patch: Path, *, check_only: bool) -> str:
    command = ["git", "-c", "core.autocrlf=false", "apply"]
    if check_only:
        command.append("--check")
    command.extend(["--whitespace=nowarn", str(patch)])
    try:
        result = subprocess.run(
            command,
            cwd=source,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
    except FileNotFoundError as exc:
        raise IntegrationError("Git is required to apply the OnePlus cleanup patch") from exc
    if result.returncode != 0:
        phase = "preflight" if check_only else "application"
        raise IntegrationError(
            f"OnePlus cleanup patch {phase} failed with exit {result.returncode}\n"
            f"{result.stdout[-4000:]}"
        )
    return result.stdout


def _known_profiles(digest: str, field: str) -> list[str]:
    return sorted(
        profile
        for profile, contract in PROFILE_CONTRACTS.items()
        if contract[field] == digest
    )


def _assert_postimage(value: bytes, expected_digest: str) -> str:
    digest = sha256_bytes(value)
    if digest != expected_digest:
        raise IntegrationError(
            "OnePlus cleanup postimage digest changed: "
            f"expected {expected_digest}, got {digest}"
        )
    text = value.decode("utf-8")
    for marker, expected_count in POSTIMAGE_MARKERS.items():
        count = text.count(marker)
        if count != expected_count:
            raise IntegrationError(
                f"OnePlus cleanup postimage marker {marker!r} occurs {count} times; "
                f"expected {expected_count}"
            )
    if REMOVED_MARKER in text:
        raise IntegrationError("OnePlus cleanup still restores the full GKI file list")
    return digest


def integrate(
    source_dir: Path,
    base: str,
    *,
    patch_path: Path | None = None,
) -> dict[str, Any]:
    if base not in PROFILE_CONTRACTS:
        raise IntegrationError(f"unsupported OnePlus base {base!r}")
    source = _require_plain_directory(source_dir, "msm-kernel source")
    target = _require_plain_file(source / TARGET_RELATIVE, "OnePlus build wrapper")
    stamp = source / STAMP_NAME

    patch = _require_plain_file(
        patch_path if patch_path is not None else REPO_ROOT / PATCH_RELATIVE,
        "repository-owned OnePlus cleanup patch",
    )
    patch_digest = sha256_file(patch)
    if patch_digest != PATCH_SHA256:
        raise IntegrationError(
            f"OnePlus cleanup patch digest changed: expected {PATCH_SHA256}, got {patch_digest}"
        )

    before = _read_source(target)
    pre_digest = sha256_bytes(before)
    contract = PROFILE_CONTRACTS[base]
    if pre_digest != contract["pre_sha256"]:
        if pre_digest == contract["post_sha256"]:
            raise IntegrationError(
                f"{target}: OnePlus cleanup is already integrated for {base}"
            )
        pre_profiles = _known_profiles(pre_digest, "pre_sha256")
        post_profiles = _known_profiles(pre_digest, "post_sha256")
        if pre_profiles or post_profiles:
            profiles = pre_profiles or post_profiles
            state = "pristine" if pre_profiles else "already-modified"
            raise IntegrationError(
                f"{target}: {state} preimage belongs to {profiles}, not {base}"
            )
        raise IntegrationError(
            f"{target}: unrecognized or already-modified OnePlus wrapper preimage "
            f"{pre_digest}"
        )
    if stamp.exists() or stamp.is_symlink():
        raise IntegrationError(f"OnePlus cleanup integration stamp already exists: {stamp}")

    git_version = _git_version()
    try:
        _run_git_apply(source, patch, check_only=True)
        _run_git_apply(source, patch, check_only=False)
        after = _read_source(target)
        post_digest = _assert_postimage(after, contract["post_sha256"])
        try:
            patch_record_path = patch.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            patch_record_path = str(patch)
        document: dict[str, Any] = {
            "schema_version": 1,
            "integration": "oneplus-build-with-bazel-header-cleanup",
            "base": base,
            "expected_source_commit": contract["commit"],
            "git_version": git_version,
            "target": {
                "path": TARGET_RELATIVE.as_posix(),
                "pre_sha256": pre_digest,
                "post_sha256": post_digest,
            },
            "patch": {
                "path": patch_record_path,
                "sha256": patch_digest,
                "tool": "git apply",
                "whitespace": "nowarn",
            },
            "cleanup": {
                "restore_scope": "headers-selected-for-symlinking",
                "pathspec_source": "stdin",
                "preserves_listed_non_headers": True,
            },
        }
        _atomic_json(stamp, document)
        return document
    except BaseException:
        if target.is_file() and target.read_bytes() != before:
            target.write_bytes(before)
        if stamp.is_file() or stamp.is_symlink():
            stamp.unlink()
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--base", required=True, choices=tuple(sorted(PROFILE_CONTRACTS)))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        document = integrate(args.source_dir, args.base)
    except IntegrationError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(document, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
