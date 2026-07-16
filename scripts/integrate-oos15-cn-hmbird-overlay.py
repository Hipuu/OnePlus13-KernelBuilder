#!/usr/bin/env python3
"""Remove the stale SCX fork hook from the locked OOS15 CN scheduler overlay.

The China OOS15 module checkout predates the equivalent Global/OOS16 cleanup
and still initializes ``oplus_task_struct.scx`` from ``sa_common.c``.  The
pinned Fengchi/HMBIRD integration deliberately removes that deprecated SCX
storage, so the stale overlay cannot compile.  This helper accepts only the
exact locked module commit, Git blob, and full-file preimage before making the
same narrow cleanup already present in the peer OnePlus sources.
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
    "vendor/oplus/kernel/cpu/sched/sched_assist/sa_common.c"
)
STAMP_RELATIVE = Path(".op13/oos15-cn-hmbird-overlay.json")

OLD_FRAGMENT = b"\n".join(
    (
        b"#ifdef CONFIG_HMBIRD_SCHED",
        b"void scx_sched_fork(struct task_struct *p)",
        b"{",
        b"\tstruct oplus_task_struct *ots = get_oplus_task_struct(p);",
        b"\tstruct oplus_task_struct *curr_ots = get_oplus_task_struct(current);",
        b"\tif (IS_ERR_OR_NULL(ots))",
        b"\t\treturn;",
        b"",
        b"\tots->scx.dsq = NULL;",
        b"\tINIT_LIST_HEAD(&ots->scx.dsq_node.fifo);",
        b"\tRB_CLEAR_NODE(&ots->scx.dsq_node.priq);",
        b"\tots->scx.flags = 0;",
        b"\tots->scx.dsq_flags = 0;",
        b"\tots->scx.sticky_cpu = -1;",
        b"\tots->scx.runnable_at = INITIAL_JIFFIES;",
        b"\tots->scx.slice = SCX_SLICE_DFL;",
        b"\tots->scx.sched_prop = 0;",
        b"\tots->scx.ext_flags = 0;",
        b"\tots->scx.prio_backup = 0;",
        b"\tots->scx.gdsq_idx = DEFAULT_CGROUP_DL_IDX;",
        b"\tmemset(&ots->scx.sts, 0, sizeof(struct scx_task_stats));",
        b"\tif (!IS_ERR_OR_NULL(curr_ots)) {",
        b"\t\tif ((curr_ots->scx.ext_flags & EXT_FLAG_RT_CHANGED) && !p->sched_reset_on_fork) {",
        b"\t\t\tots->scx.ext_flags |= EXT_FLAG_RT_CHANGED;",
        b"\t\t\tots->scx.prio_backup = curr_ots->scx.prio_backup;",
        b"\t\t}",
        b"\t\tif (curr_ots->scx.ext_flags & EXT_FLAG_CFS_CHANGED)",
        b"\t\t\tots->scx.ext_flags |= EXT_FLAG_CFS_CHANGED;",
        b"\t}",
        b"}",
        b"#endif",
        b"",
        b"/* register vender hook in kernel/sched/core.c */",
        b"void android_rvh_sched_fork_handler(void *unused, struct task_struct *p)",
        b"{",
        b"\tinit_task_ux_info(p);",
        b"#ifdef CONFIG_HMBIRD_SCHED",
        b"\tif(HMBIRD_GKI_VERSION == get_hmbird_version_type()) {",
        b"\t\tscx_sched_fork(p);",
        b"\t}",
        b"#endif",
        b"}",
    )
) + b"\n"

NEW_FRAGMENT = b"\n".join(
    (
        b"/* register vender hook in kernel/sched/core.c */",
        b"void android_rvh_sched_fork_handler(void *unused, struct task_struct *p)",
        b"{",
        b"\tinit_task_ux_info(p);",
        b"}",
    )
) + b"\n"

FORBIDDEN_POSTIMAGE_TOKENS = (
    b"scx_sched_fork",
    b"ots->scx",
    b"SCX_SLICE_DFL",
    b"struct scx_task_stats",
)


@dataclass(frozen=True)
class OverlayContract:
    base: str
    source_commit: str
    target_blob: str
    post_blob: str
    pre_sha256: str
    post_sha256: str
    pre_size: int
    post_size: int
    mode: int
    peer_sources: Mapping[str, Mapping[str, str]]


CONTRACT = OverlayContract(
    base="oos15-cn",
    source_commit="a85bac41e21a790e216039cde1d34a6c5d6416d1",
    target_blob="625b526e0c234212152b46a0e5b874368f5a3902",
    post_blob="6e138d4b1903b361f507d40ad1a01a6f1fdcc514",
    pre_sha256="96b1a2cfe793bc33f1e6c942058767587d95ff4317b8811a305855fd570123af",
    post_sha256="df731638c1e525b2ae330fa36738f80f48b24d09a1d099e21f314b4ca005dd63",
    pre_size=59516,
    post_size=58398,
    mode=0o755,
    peer_sources=MappingProxyType(
        {
            "oos15-global": MappingProxyType(
                {
                    "source_commit": "94abfbeabb2b95ab17a560349970565ecfe0c1a1",
                    "target_blob": "b71456b0ba0e1809f5d6a8700c0bbb8c937c99c3",
                }
            ),
            "oos16": MappingProxyType(
                {
                    "source_commit": "9e08115481a5ee85ee44690bdcbf34d747928a23",
                    "target_blob": "b88c1c770be4028af87f88fef668658efe5a3b10",
                }
            ),
        }
    ),
)


class IntegrationError(RuntimeError):
    """The scheduler overlay does not match the locked compatibility contract."""


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def git_blob_oid(payload: bytes) -> str:
    header = f"blob {len(payload)}\0".encode("ascii")
    return hashlib.sha1(header + payload, usedforsecurity=False).hexdigest()


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


def _run_git(source: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(source), *arguments],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError as exc:
        raise IntegrationError("Git is required to verify the module checkout") from exc
    value = result.stdout.strip()
    if result.returncode != 0:
        detail = result.stderr.strip() or value
        raise IntegrationError(f"Git verification failed: {detail}")
    return value


def _git_oid(source: Path, revision: str, label: str) -> str:
    value = _run_git(source, "rev-parse", "--verify", revision)
    if len(value) != 40 or any(character not in "0123456789abcdef" for character in value):
        raise IntegrationError(f"invalid {label} object ID: {value!r}")
    return value


def _read_source(path: Path) -> bytes:
    payload = path.read_bytes()
    if b"\x00" in payload:
        raise IntegrationError(f"scheduler overlay contains a NUL byte: {path}")
    if b"\r" in payload:
        raise IntegrationError(f"scheduler overlay is not LF-only: {path}")
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise IntegrationError(f"scheduler overlay is not UTF-8: {path}") from exc
    if not payload.endswith(b"\n"):
        raise IntegrationError(f"scheduler overlay lacks its final LF: {path}")
    return payload


def _rewrite(payload: bytes, contract: OverlayContract) -> bytes:
    digest = sha256_bytes(payload)
    if digest == contract.post_sha256 and len(payload) == contract.post_size:
        raise IntegrationError("OOS15 CN HMBIRD scheduler overlay is already integrated")
    if len(payload) != contract.pre_size:
        raise IntegrationError(
            "OOS15 CN HMBIRD scheduler overlay size changed: "
            f"expected {contract.pre_size}, got {len(payload)}"
        )
    if digest != contract.pre_sha256:
        raise IntegrationError(
            "OOS15 CN HMBIRD scheduler overlay preimage changed: "
            f"expected {contract.pre_sha256}, got {digest}"
        )
    occurrences = payload.count(OLD_FRAGMENT)
    if occurrences != 1:
        raise IntegrationError(
            f"stale SCX scheduler fragment occurs {occurrences} times; expected 1"
        )
    result = payload.replace(OLD_FRAGMENT, NEW_FRAGMENT, 1)
    post_digest = sha256_bytes(result)
    if post_digest != contract.post_sha256:
        raise IntegrationError(
            "OOS15 CN HMBIRD scheduler overlay postimage changed: "
            f"expected {contract.post_sha256}, got {post_digest}"
        )
    if len(result) != contract.post_size:
        raise IntegrationError(
            "OOS15 CN HMBIRD scheduler overlay postimage size changed: "
            f"expected {contract.post_size}, got {len(result)}"
        )
    post_blob = git_blob_oid(result)
    if post_blob != contract.post_blob:
        raise IntegrationError(
            "OOS15 CN HMBIRD scheduler overlay postimage blob changed: "
            f"expected {contract.post_blob}, got {post_blob}"
        )
    for token in FORBIDDEN_POSTIMAGE_TOKENS:
        if token in result:
            raise IntegrationError(
                f"OOS15 CN HMBIRD scheduler overlay retains {token.decode()!r}"
            )
    if result.count(NEW_FRAGMENT) != 1:
        raise IntegrationError("clean scheduler fork handler is not unique")
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


def integrate(
    source_dir: Path,
    base: str,
    *,
    contract: OverlayContract = CONTRACT,
) -> dict[str, Any]:
    if base != contract.base:
        raise IntegrationError(
            f"scheduler overlay repair is only valid for {contract.base}, got {base!r}"
        )
    source = _require_plain_directory(source_dir, "modules-and-devicetree checkout")
    target = _require_plain_file(source / TARGET_RELATIVE, "scheduler overlay")
    metadata = _require_plain_directory(source / STAMP_RELATIVE.parent, "builder metadata")
    stamp = metadata / STAMP_RELATIVE.name
    if stamp.exists() or stamp.is_symlink():
        raise IntegrationError(f"scheduler overlay integration stamp already exists: {stamp}")

    source_commit = _git_oid(source, "HEAD", "module commit")
    if source_commit != contract.source_commit:
        raise IntegrationError(
            f"module commit changed: expected {contract.source_commit}, got {source_commit}"
        )
    target_blob = _git_oid(
        source,
        f"HEAD:{TARGET_RELATIVE.as_posix()}",
        "scheduler overlay blob",
    )
    if target_blob != contract.target_blob:
        raise IntegrationError(
            f"scheduler overlay blob changed: expected {contract.target_blob}, got {target_blob}"
        )

    mode = stat.S_IMODE(target.stat().st_mode)
    if mode != contract.mode:
        raise IntegrationError(
            f"scheduler overlay mode changed: expected {contract.mode:04o}, got {mode:04o}"
        )
    before = _read_source(target)
    after = _rewrite(before, contract)
    try:
        _atomic_bytes(target, after, mode)
        if _read_source(target) != after:
            raise IntegrationError("scheduler overlay write verification failed")
        document: dict[str, Any] = {
            "schema_version": 1,
            "integration": "oneplus-oos15-cn-hmbird-scheduler-overlay",
            "base": base,
            "source_commit": source_commit,
            "target": {
                "path": TARGET_RELATIVE.as_posix(),
                "blob": target_blob,
                "post_blob": contract.post_blob,
                "pre_sha256": contract.pre_sha256,
                "post_sha256": contract.post_sha256,
                "pre_size": contract.pre_size,
                "post_size": contract.post_size,
                "mode": f"{mode:04o}",
            },
            "repair": {
                "removed": "deprecated scx_sched_fork overlay hook",
                "replacement": "init_task_ux_info-only fork handler",
                "old_fragment_sha256": sha256_bytes(OLD_FRAGMENT),
                "new_fragment_sha256": sha256_bytes(NEW_FRAGMENT),
            },
            "peer_sources": {
                name: dict(values)
                for name, values in sorted(contract.peer_sources.items())
            },
        }
        _atomic_json(stamp, document)
        return document
    except BaseException:
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
