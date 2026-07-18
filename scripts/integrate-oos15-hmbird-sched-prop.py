#!/usr/bin/env python3
"""Redirect the locked OOS15 HMBIRD sched-prop compatibility header.

The pinned CN and Global Fengchi patches create the same executable
``sched_ext.h`` compatibility header in both kernel trees.  That header still
addresses ``oplus_task_struct.scx.sched_prop`` even though Fengchi moves the
field to ``hmbird_entity``.  Accept only the exact generated preimage and
verified vendor-HMBIRD stamp, then rewrite the three helpers in both trees as
one rollback-safe transaction.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


TARGET_RELATIVE = Path("include/linux/sched/sched_ext.h")
STAMP_RELATIVE = Path(".op13/oos15-hmbird-sched-prop.json")
VENDOR_STAMP_RELATIVE = Path(
    "kernel_platform/msm-kernel/.op13-hmbird-vendor.json"
)
TREE_RELATIVES = MappingProxyType(
    {
        "common": Path("kernel_platform/common"),
        "msm-kernel": Path("kernel_platform/msm-kernel"),
    }
)

PREIMAGE = b"""/* SPDX-License-Identifier: GPL-2.0-only */
/*
 * Copyright (C) 2024 Oplus. All rights reserved.
 */
#ifndef _OPLUS_SCHED_EXT_H
#define _OPLUS_SCHED_EXT_H
#include "sa_common.h"

#define SCHED_PROP_TOP_THREAD_SHIFT (8)
#define SCHED_PROP_TOP_THREAD_MASK  (0xf << SCHED_PROP_TOP_THREAD_SHIFT)
#define SCHED_PROP_DEADLINE_MASK (0xFF) /* deadline for ext sched class */
#define SCHED_PROP_DEADLINE_LEVEL1 (1)  /* 1ms for user-aware audio tasks */
#define SCHED_PROP_DEADLINE_LEVEL2 (2)  /* 2ms for user-aware touch tasks */
#define SCHED_PROP_DEADLINE_LEVEL3 (3)  /* 4ms for user aware dispaly tasks */
#define SCHED_PROP_DEADLINE_LEVEL4 (4)  /* 6ms */
#define SCHED_PROP_DEADLINE_LEVEL5 (5)  /* 8ms */
#define SCHED_PROP_DEADLINE_LEVEL6 (6)  /* 16ms */
#define SCHED_PROP_DEADLINE_LEVEL7 (7)  /* 32ms */
#define SCHED_PROP_DEADLINE_LEVEL8 (8)  /* 64ms */
#define SCHED_PROP_DEADLINE_LEVEL9 (9)  /* 128ms */

static inline int sched_prop_get_top_thread_id(struct task_struct *p)
{
	struct oplus_task_struct *ots = get_oplus_task_struct(p);

	if (!ots) {
		return -EPERM;
	}

	return ((ots->scx.sched_prop & SCHED_PROP_TOP_THREAD_MASK) >> SCHED_PROP_TOP_THREAD_SHIFT);
}

static inline int sched_set_sched_prop(struct task_struct *p, unsigned long sp)
{
	struct oplus_task_struct *ots = get_oplus_task_struct(p);

	if (!ots) {
		pr_err("scx_sched_ext: sched_set_sched_prop failed! fn=%s\\n", __func__);
		return -EPERM;
	}

	ots->scx.sched_prop = sp;
	return 0;
}

static inline unsigned long sched_get_sched_prop(struct task_struct *p)
{
	struct oplus_task_struct *ots = get_oplus_task_struct(p);

	if (!ots) {
		pr_err("scx_sched_ext: sched_get_sched_prop failed! fn=%s\\n", __func__);
		return (unsigned long)-1;
	}
	return ots->scx.sched_prop;
}

#endif /*_OPLUS_SCHED_EXT_H */
"""

OLD_DECLARATION = b"struct oplus_task_struct *ots = get_oplus_task_struct(p);"
NEW_DECLARATION = b"struct hmbird_entity *entity = p ? get_hmbird_ts(p) : NULL;"
OLD_NULL_CHECK = b"if (!ots)"
NEW_NULL_CHECK = b"if (!entity)"
OLD_ACCESS = b"ots->scx.sched_prop"
NEW_ACCESS = b"entity->sched_prop"
POSTIMAGE = (
    PREIMAGE.replace(OLD_DECLARATION, NEW_DECLARATION)
    .replace(OLD_NULL_CHECK, NEW_NULL_CHECK)
    .replace(OLD_ACCESS, NEW_ACCESS)
)


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def git_blob_oid(payload: bytes) -> str:
    header = f"blob {len(payload)}\0".encode("ascii")
    return hashlib.sha1(header + payload, usedforsecurity=False).hexdigest()


@dataclass(frozen=True)
class HeaderContract:
    bases: tuple[str, ...]
    wild_commit: str
    vendor_commits: Mapping[str, str]
    main_patches: Mapping[str, Mapping[str, str]]
    compatibility_patch: str
    compatibility_sha256: str
    pre_blob: str
    post_blob: str
    pre_sha256: str
    post_sha256: str
    pre_size: int
    post_size: int
    mode: int


CONTRACT = HeaderContract(
    bases=("oos15-cn", "oos15-global"),
    wild_commit="2ee34500cb4c3ee954ba36090e11f6ff08b3ec2f",
    vendor_commits=MappingProxyType(
        {
            "oos15-cn": "d09a875fd283664a4ad3a8722fb608356985dab1",
            "oos15-global": "59336d4db04efdc70e1c63d6a92f7e4d14efafa8",
        }
    ),
    main_patches=MappingProxyType(
        {
            "oos15-cn": MappingProxyType(
                {
                    "path": "oneplus/hmbird/deprecated/fengchi_OP13_A15.patch",
                    "sha256": "91ec3d4a6e423202dfff812746a518d55cc4b90d47bafcb85d25f89af7ba2f4f",
                }
            ),
            "oos15-global": MappingProxyType(
                {
                    "path": "oneplus/hmbird/fengchi_OP13-CPH_A15.patch",
                    "sha256": "885f9cbfbe63dd57e2f681e17a8a1e4be7e18c37d8c48de1dab6a44db672199b",
                }
            ),
        }
    ),
    compatibility_patch="patches/oneplus13/hmbird/vendor-preimage-oos15.patch",
    compatibility_sha256="26ddc31a46979eda7954378245ef12a5fca9d973bfe7455b2db109f857699e81",
    pre_blob="9f1afdb8a04b183e12ea882633839bd045f54934",
    post_blob="f243c05334836b5b7340c4f45fc76f91a15714fa",
    pre_sha256="a013e0caa83a1ec80efd0b0c5f6cca06aeefa0a4449e5bc678ec95392de64b84",
    post_sha256="5727cadb1e7293bc7e78e159135f9e83bd0648cbd6c056a219168da00777c803",
    pre_size=1776,
    post_size=1788,
    mode=0o755,
)


class IntegrationError(RuntimeError):
    """The generated OOS15 compatibility header violates its lock."""


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


def _read_source(path: Path) -> bytes:
    payload = path.read_bytes()
    if b"\x00" in payload or b"\r" in payload:
        raise IntegrationError(f"HMBIRD sched-prop header is not LF-only text: {path}")
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise IntegrationError(f"HMBIRD sched-prop header is not UTF-8: {path}") from exc
    if not payload.endswith(b"\n"):
        raise IntegrationError(f"HMBIRD sched-prop header lacks its final LF: {path}")
    return payload


def _rewrite(payload: bytes, contract: HeaderContract) -> bytes:
    digest = sha256_bytes(payload)
    if digest == contract.post_sha256 and len(payload) == contract.post_size:
        raise IntegrationError("OOS15 HMBIRD sched-prop header is already integrated")
    if len(payload) != contract.pre_size:
        raise IntegrationError(
            "OOS15 HMBIRD sched-prop header size changed: "
            f"expected {contract.pre_size}, got {len(payload)}"
        )
    if digest != contract.pre_sha256 or git_blob_oid(payload) != contract.pre_blob:
        raise IntegrationError(
            "OOS15 HMBIRD sched-prop header preimage changed: "
            f"expected {contract.pre_sha256}, got {digest}"
        )
    for token, count in (
        (OLD_DECLARATION, 3),
        (OLD_NULL_CHECK, 3),
        (OLD_ACCESS, 3),
    ):
        occurrences = payload.count(token)
        if occurrences != count:
            raise IntegrationError(
                f"HMBIRD sched-prop token {token.decode()!r} occurs "
                f"{occurrences} times; expected {count}"
            )
    result = (
        payload.replace(OLD_DECLARATION, NEW_DECLARATION)
        .replace(OLD_NULL_CHECK, NEW_NULL_CHECK)
        .replace(OLD_ACCESS, NEW_ACCESS)
    )
    if (
        len(result) != contract.post_size
        or sha256_bytes(result) != contract.post_sha256
        or git_blob_oid(result) != contract.post_blob
    ):
        raise IntegrationError("OOS15 HMBIRD sched-prop postimage changed")
    if OLD_DECLARATION in result or OLD_NULL_CHECK in result or OLD_ACCESS in result:
        raise IntegrationError("OOS15 HMBIRD sched-prop postimage retains SCX storage")
    if result.count(NEW_DECLARATION) != 3 or result.count(NEW_ACCESS) != 3:
        raise IntegrationError("OOS15 HMBIRD sched-prop redirection is incomplete")
    return result


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


def _verify_vendor_stamp(
    source: Path,
    base: str,
    contract: HeaderContract,
) -> dict[str, Any]:
    path = _require_plain_file(source / VENDOR_STAMP_RELATIVE, "vendor HMBIRD stamp")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrationError("vendor HMBIRD stamp is not valid UTF-8 JSON") from exc
    expected = contract.main_patches[base]
    inputs = document.get("inputs")
    if (
        document.get("schema_version") != 1
        or document.get("integration") != "oneplus-vendor-hmbird-fengchi"
        or document.get("base") != base
        or not isinstance(inputs, dict)
        or inputs.get("vendor_commit") != contract.vendor_commits[base]
        or inputs.get("wild_commit") != contract.wild_commit
        or inputs.get("main_patch") != expected["path"]
        or inputs.get("main_patch_sha256") != expected["sha256"]
        or inputs.get("compatibility_patch") != contract.compatibility_patch
        or inputs.get("compatibility_patch_sha256")
        != contract.compatibility_sha256
    ):
        raise IntegrationError("vendor HMBIRD stamp does not match the locked OOS15 input")
    return document


def integrate(
    source_dir: Path,
    base: str,
    *,
    contract: HeaderContract = CONTRACT,
) -> dict[str, Any]:
    if base not in contract.bases or base not in contract.main_patches:
        raise IntegrationError(
            "sched-prop repair is only valid for locked OOS15 CN/Global bases, "
            f"got {base!r}"
        )
    source = _require_plain_directory(source_dir, "locked source checkout")
    metadata = _require_plain_directory(source / STAMP_RELATIVE.parent, "builder metadata")
    stamp = metadata / STAMP_RELATIVE.name
    if stamp.exists() or stamp.is_symlink():
        raise IntegrationError(f"sched-prop integration stamp already exists: {stamp}")
    _verify_vendor_stamp(source, base, contract)

    targets: dict[str, Path] = {}
    snapshots: dict[str, tuple[bytes, int]] = {}
    postimages: dict[str, bytes] = {}
    for tree, relative in TREE_RELATIVES.items():
        target = _require_plain_file(
            source / relative / TARGET_RELATIVE,
            f"{tree} HMBIRD sched-prop header",
        )
        mode = stat.S_IMODE(target.stat().st_mode)
        if mode != contract.mode:
            raise IntegrationError(
                f"{tree} HMBIRD sched-prop mode changed: "
                f"expected {contract.mode:04o}, got {mode:04o}"
            )
        before = _read_source(target)
        targets[tree] = target
        snapshots[tree] = (before, mode)
        postimages[tree] = _rewrite(before, contract)

    try:
        for tree in sorted(targets):
            before, mode = snapshots[tree]
            after = postimages[tree]
            _atomic_bytes(targets[tree], after, mode)
            if _read_source(targets[tree]) != after:
                raise IntegrationError(f"{tree} sched-prop write verification failed")
            if before != PREIMAGE or after != POSTIMAGE:
                raise IntegrationError(f"{tree} sched-prop literal contract changed")

        document: dict[str, Any] = {
            "schema_version": 1,
            "integration": "oneplus-oos15-hmbird-sched-prop",
            "base": base,
            "inputs": {
                "vendor_commit": contract.vendor_commits[base],
                "wild_commit": contract.wild_commit,
                "main_patch": dict(contract.main_patches[base]),
                "compatibility_patch": {
                    "path": contract.compatibility_patch,
                    "sha256": contract.compatibility_sha256,
                },
            },
            "targets": {
                tree: {
                    "path": (TREE_RELATIVES[tree] / TARGET_RELATIVE).as_posix(),
                    "pre_blob": contract.pre_blob,
                    "post_blob": contract.post_blob,
                    "pre_sha256": contract.pre_sha256,
                    "post_sha256": contract.post_sha256,
                    "pre_size": contract.pre_size,
                    "post_size": contract.post_size,
                    "mode": f"{snapshots[tree][1]:04o}",
                }
                for tree in sorted(targets)
            },
            "repair": {
                "removed": "oplus_task_struct.scx.sched_prop compatibility access",
                "replacement": "null-checked hmbird_entity.sched_prop access",
                "helper_count": 3,
            },
        }
        _atomic_json(stamp, document)
        return document
    except BaseException:
        for tree, target in targets.items():
            before, mode = snapshots[tree]
            if target.is_file() and target.read_bytes() != before:
                _atomic_bytes(target, before, mode)
        stamp.unlink(missing_ok=True)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--base", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        document = integrate(args.source_dir, args.base)
    except IntegrationError as exc:
        print(f"integration error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(document, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
