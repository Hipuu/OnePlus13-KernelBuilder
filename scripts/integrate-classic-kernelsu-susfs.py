#!/usr/bin/env python3
"""Fix two direct SUSFS wrapper calls in the pinned classic KernelSU tree."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any


EXPECTED_KSU_COMMIT = "b0bc817b4e966aa6aa830834eaf6ef765d821d40"
EXPECTED_SUSFS_COMMIT = "a8c720c42ca46fca13179280b13aa13c9fbe1562"
STAGE_STAMP = ".op13-root-stage.json"
OUTPUT_STAMP = ".op13-classic-susfs-compat.json"
TARGET = Path("kernel/feature/selinux_hide.c")
PRE_SIZE = 24646
PRE_SHA256 = "cb482202cf784394e7d8c2f1cc8b04dc1aa74396927e896e49b5b5c80d070b8c"
POST_SIZE = 24459
POST_SHA256 = "c53265a24599570dd07af2f25b3dc7120f722ba5f70f9a30a91fbbecb16ad78a"
REPLACEMENTS = (
    (
        b"    if (security_dump_masked_av_fn)\n"
        b"        security_dump_masked_av_fn(policydb, scontext, tcontext, "
        b"tclass, masked, \"bounds\");\n",
        b"    security_dump_masked_av_fn(policydb, scontext, tcontext, "
        b"tclass, masked, \"bounds\");\n",
    ),
    (
        b"    if (context_struct_compute_av_fn) {\n"
        b"        context_struct_compute_av_fn(policydb, scontext, tcontext, "
        b"tclass, avd, NULL);\n"
        b"    } else {\n"
        b"        context_struct_compute_av(policydb, scontext, tcontext, "
        b"tclass, avd, NULL);\n"
        b"    }\n",
        b"    context_struct_compute_av_fn(policydb, scontext, tcontext, "
        b"tclass, avd, NULL);\n",
    ),
)


class IntegrationError(RuntimeError):
    """The pinned classic KernelSU/SUSFS compatibility contract changed."""


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_json(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise IntegrationError(f"{label} is missing or unsafe: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise IntegrationError(f"{label} is invalid: {path}") from exc
    if not isinstance(value, dict):
        raise IntegrationError(f"{label} must contain a JSON object")
    return value


def _atomic_write(path: Path, payload: bytes, *, mode: int) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _transform(payload: bytes) -> bytes:
    updated = payload
    for old, new in REPLACEMENTS:
        if updated.count(old) != 1:
            raise IntegrationError(
                "classic KernelSU SUSFS function-address guard contract changed"
            )
        updated = updated.replace(old, new)
    return updated


def integrate(ksu_dir: Path) -> dict[str, Any]:
    raw_root = Path(ksu_dir)
    if raw_root.is_symlink():
        raise IntegrationError(f"staged KernelSU directory is a symlink: {raw_root}")
    try:
        root = raw_root.resolve(strict=True)
    except OSError as exc:
        raise IntegrationError(f"staged KernelSU directory is missing: {raw_root}") from exc
    if not root.is_dir():
        raise IntegrationError(f"staged KernelSU path is not a directory: {root}")

    stage = _read_json(root / STAGE_STAMP, "KernelSU stage stamp")
    if (
        stage.get("variant") != "kernelsu"
        or stage.get("source_commit") != EXPECTED_KSU_COMMIT
    ):
        raise IntegrationError("classic KernelSU stage provenance changed")

    target = root / TARGET
    if target.is_symlink() or not target.is_file():
        raise IntegrationError(f"classic KernelSU SELinux source is missing: {target}")
    try:
        target.resolve(strict=True).relative_to(root)
    except (OSError, ValueError) as exc:
        raise IntegrationError("classic KernelSU SELinux source escapes its tree") from exc

    original = target.read_bytes()
    identity = (len(original), _sha256(original))
    if identity == (POST_SIZE, POST_SHA256):
        updated = original
        status = "already-integrated"
    elif identity == (PRE_SIZE, PRE_SHA256):
        updated = _transform(original)
        status = "integrated"
    else:
        raise IntegrationError(
            "classic KernelSU post-SUSFS pre/postimage changed; "
            f"got {identity[0]} bytes/{identity[1]}"
        )
    if len(updated) != POST_SIZE or _sha256(updated) != POST_SHA256:
        raise IntegrationError("classic KernelSU SUSFS compatibility postimage changed")
    for old, _ in REPLACEMENTS:
        if old in updated:
            raise IntegrationError("classic KernelSU function-address guard remains")

    document: dict[str, Any] = {
        "schema_version": 1,
        "integration": "classic-kernelsu-susfs-direct-wrapper-calls",
        "kernelsu_commit": EXPECTED_KSU_COMMIT,
        "susfs_commit": EXPECTED_SUSFS_COMMIT,
        "path": TARGET.as_posix(),
        "status": status,
        "pre_size": PRE_SIZE,
        "pre_sha256": PRE_SHA256,
        "post_size": POST_SIZE,
        "post_sha256": POST_SHA256,
        "diagnostics": [
            "security_dump_masked_av_fn always-true address check",
            "context_struct_compute_av_fn always-true address check",
        ],
    }
    stamp = root / OUTPUT_STAMP
    stamp_payload = (
        json.dumps(document, indent=2, sort_keys=True, separators=(",", ": "))
        + "\n"
    ).encode("utf-8")
    mode = target.stat().st_mode & 0o777
    changed = updated != original
    try:
        if changed:
            _atomic_write(target, updated, mode=mode)
        if target.read_bytes() != updated:
            raise IntegrationError("classic KernelSU committed postimage changed")
        _atomic_write(stamp, stamp_payload, mode=0o644)
    except (OSError, IntegrationError) as exc:
        if changed:
            try:
                _atomic_write(target, original, mode=mode)
            except OSError as rollback_exc:
                raise IntegrationError(
                    f"classic KernelSU compatibility failed: {exc}; "
                    f"rollback failed: {rollback_exc}"
                ) from exc
        raise IntegrationError(
            f"classic KernelSU compatibility integration failed: {exc}"
        ) from exc
    return document


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ksu-dir", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        document = integrate(args.ksu_dir)
    except IntegrationError as exc:
        print(f"classic KernelSU SUSFS compatibility: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(document, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
