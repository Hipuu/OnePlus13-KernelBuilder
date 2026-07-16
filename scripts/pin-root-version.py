#!/usr/bin/env python3
"""Pin Git-derived KernelSU metadata after the audited SUSFS integration."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any


ANCHOR = (
    "KSU_NEW_DCACHE_FLUSH := $(shell grep -q __flush_dcache_area "
    "$(srctree)/arch/arm64/include/asm/cacheflush.h ; echo $$?)\n"
)
PINS: dict[str, dict[str, Any]] = {
    "kernelsu": {
        "commit": "b0bc817b4e966aa6aa830834eaf6ef765d821d40",
        "version": 32525,
        "tag": "v3.2.5",
        "history_count": 2525,
        "pre_sha256": "b6c1b93c2bab2dc46580db6f1c2c63a3960bf1801917c04bc6fcc75a7b8d84bb",
        "block_start": "MDIR := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))\n",
        "block_sha256": "6f784cb1ba42d2019c6c736418629c43730a84e15a7200220572217dbbf093ff",
        "replacement": (
            "# Pinned source-only metadata for KernelSU v3.2.5 "
            "(b0bc817b4e966aa6aa830834eaf6ef765d821d40)\n"
            "KSU_VERSION := 32525\n"
            "$(info -- KernelSU version: $(KSU_VERSION))\n"
            "ccflags-y += -DKSU_VERSION=$(KSU_VERSION)\n\n"
        ),
        "post_sha256": "b374339ca45c9281f0f3c97781f01736e51334afe712403cfe7c491ae6c70158",
    },
    "kernelsu-next": {
        "commit": "1a0ef4898568a013b51d74ceb5593b83725bfb78",
        "version": 33207,
        "tag": "v3.2.0",
        "history_count": 3207,
        "pre_sha256": "9a309a41d71f0af221ab9300f1628ee6586c82aa4e786ea72e17c898f45c28d4",
        "block_start": 'LPATH := /usr/bin/env PATH="$$PATH":/usr/bin:/usr/local/bin\n',
        "block_sha256": "15e99a8be8c7f1290e0e299b7ca95153ce028dca644bf4dd7d321e6fac082f55",
        "replacement": (
            "# Pinned source-only metadata for KernelSU-Next dev "
            "(1a0ef4898568a013b51d74ceb5593b83725bfb78)\n"
            "KSU_VERSION := 33207\n"
            "KSU_VERSION_TAG := v3.2.0\n"
            "$(info -- KernelSU-Next version: $(KSU_VERSION))\n"
            "ccflags-y += -DKSU_VERSION=$(KSU_VERSION)\n"
            "$(info -- KernelSU-Next tag: $(KSU_VERSION_TAG))\n"
            'ccflags-y += -DKSU_VERSION_TAG=\\"$(KSU_VERSION_TAG)\\"\n\n'
        ),
        "post_sha256": "cf9a57a9526334d2783ebd0a72566494bb8c5885890d5b548703196a7a636438",
    },
}
STAGE_STAMP = ".op13-root-stage.json"
INTEGRATION_STAMP = ".op13-susfs-integrated.json"
VERSION_STAMP = ".op13-root-version.json"


class PinError(RuntimeError):
    pass


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _plain_directory(path: Path, label: str) -> Path:
    if path.is_symlink():
        raise PinError(f"{label} must not be a symbolic link: {path}")
    resolved = path.resolve()
    if not resolved.is_dir():
        raise PinError(f"{label} is missing: {resolved}")
    return resolved


def _read_json(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise PinError(f"{label} is missing or unsafe: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PinError(f"{label} is invalid: {path}") from exc
    if not isinstance(value, dict):
        raise PinError(f"{label} must contain a JSON object")
    return value


def _atomic_write(path: Path, payload: bytes, mode: int = 0o600) -> None:
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


def pin(workspace: Path, root_dir: Path, variant: str) -> dict[str, Any]:
    spec = PINS.get(variant)
    if spec is None:
        raise PinError(f"unsupported root version variant: {variant}")
    workspace = _plain_directory(Path(workspace), "workspace")
    root_dir = _plain_directory(Path(root_dir), "staged root source")
    if root_dir == workspace or not _inside(root_dir, workspace):
        raise PinError("staged root source escapes the workspace")

    stamp_path = root_dir / VERSION_STAMP
    if stamp_path.exists() or stamp_path.is_symlink():
        raise PinError(f"root version stamp already exists: {stamp_path}")
    stage = _read_json(root_dir / STAGE_STAMP, "root stage stamp")
    if stage.get("variant") != variant or stage.get("source_commit") != spec["commit"]:
        raise PinError("root stage provenance does not match the version pin")
    if variant == "kernelsu-next":
        integration = _read_json(root_dir / INTEGRATION_STAMP, "KernelSU-Next integration stamp")
        if integration.get("integration") != "kernelsu-next-susfs-v2.2.0":
            raise PinError("KernelSU-Next SUSFS integration provenance changed")

    kbuild = root_dir / "kernel" / "Kbuild"
    if kbuild.is_symlink() or not kbuild.is_file():
        raise PinError(f"root Kbuild is missing or unsafe: {kbuild}")
    original = kbuild.read_bytes()
    actual_pre = _sha256(original)
    if actual_pre != spec["pre_sha256"]:
        raise PinError(
            f"{variant} post-SUSFS Kbuild changed: expected {spec['pre_sha256']}, got {actual_pre}"
        )

    start_marker = str(spec["block_start"]).encode("utf-8")
    anchor = ANCHOR.encode("utf-8")
    if original.count(start_marker) != 1 or original.count(anchor) != 1:
        raise PinError(f"{variant} version block anchors changed")
    start = original.index(start_marker)
    end = original.index(anchor)
    if end <= start:
        raise PinError(f"{variant} version block anchors are out of order")
    old_block = original[start:end]
    actual_block = _sha256(old_block)
    if actual_block != spec["block_sha256"]:
        raise PinError(
            f"{variant} version block changed: expected {spec['block_sha256']}, got {actual_block}"
        )
    replacement = str(spec["replacement"]).encode("utf-8")
    updated = original[:start] + replacement + original[end:]
    actual_post = _sha256(updated)
    if actual_post != spec["post_sha256"]:
        raise PinError(
            f"{variant} pinned Kbuild digest mismatch: expected {spec['post_sha256']}, got {actual_post}"
        )

    mode = stat.S_IMODE(kbuild.stat().st_mode)
    _atomic_write(kbuild, updated, mode)
    document: dict[str, Any] = {
        "schema_version": 1,
        "variant": variant,
        "source_commit": spec["commit"],
        "tag": spec["tag"],
        "history_count": spec["history_count"],
        "version": spec["version"],
        "formula": f"30000 + {spec['history_count']}",
        "kbuild_pre_sha256": actual_pre,
        "version_block_sha256": actual_block,
        "kbuild_post_sha256": actual_post,
    }
    _atomic_write(
        stamp_path,
        (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    return document


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--root-dir", type=Path, required=True)
    parser.add_argument("--variant", choices=tuple(sorted(PINS)), required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        document = pin(args.workspace, args.root_dir, args.variant)
    except PinError as exc:
        print(f"pin error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(document, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
