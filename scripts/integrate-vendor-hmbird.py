#!/usr/bin/env python3
"""Apply the pinned Fengchi/HMBIRD patch to an exact OnePlus vendor kernel.

The upstream patches are preserved byte-for-byte from the locked Wild checkout.
Small, locally audited compatibility patches first restore the exact preimages
expected by Fengchi. Both compatibility and upstream patches are then replayed
with GNU patch fuzz disabled.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping


EXPECTED_WILD_COMMIT = "2ee34500cb4c3ee954ba36090e11f6ff08b3ec2f"
STAMP_NAME = ".op13-hmbird-vendor.json"

SCX_WORKTREE_SHA256 = {
    "include/linux/sched/ext.h": "160a38a5ad30ba403b9a7c0e100d0bc40bfcfbc2e8facc8e3087fe2022f9e94e",
    "kernel/sched/ext.c": "abbd35e0cb0bc15a5069e76d74cc30ac7997599405958dd5fda359df8c010492",
    "kernel/sched/ext.h": "0560166c0ed55152312502fbf8583c3a50a5f5807e9c20ba5707ed5e31ee856e",
}

FORBIDDEN_OUTPUT_TOKENS = {
    "include/linux/sched.h": (
        "struct sched_ext_entity *scx",
        "CONFIG_SLIM_SCHED",
    ),
    "init/init_task.c": (".scx",),
    "kernel/sched/build_policy.c": ('"ext.c"',),
    "kernel/sched/core.c": (
        "SCHED_CHANGE_BLOCK",
        "&ext_sched_class",
    ),
}

REQUIRED_OUTPUT_TOKENS = {
    "arch/arm64/configs/defconfig": ("CONFIG_HMBIRD_SCHED=y",),
    "arch/arm64/configs/gki_defconfig": ("CONFIG_HMBIRD_SCHED=y",),
    "kernel/Kconfig.preempt": ("config HMBIRD_SCHED",),
    "kernel/sched/core.c": ("hmbird_sched_class",),
}


@dataclass(frozen=True)
class BaseSpec:
    vendor_commit: str
    main_patch: Path
    main_sha256: str
    compatibility_patch: Path
    compatibility_sha256: str
    preimage_blobs: Mapping[str, str]
    output_sha256: Mapping[str, str]


BASE_SPECS: Mapping[str, BaseSpec] = {
    "oos15-cn": BaseSpec(
        vendor_commit="d09a875fd283664a4ad3a8722fb608356985dab1",
        main_patch=Path("oneplus/hmbird/deprecated/fengchi_OP13_A15.patch"),
        main_sha256="91ec3d4a6e423202dfff812746a518d55cc4b90d47bafcb85d25f89af7ba2f4f",
        compatibility_patch=Path("patches/oneplus13/hmbird/vendor-preimage-oos15.patch"),
        compatibility_sha256="26ddc31a46979eda7954378245ef12a5fca9d973bfe7455b2db109f857699e81",
        preimage_blobs={
            "include/linux/sched.h": "6a7adaec263bbaa4665b41837f3737a11155a208",
            "include/linux/sched/ext.h": "31c45aa285a5a82751dacdf92153c63d553fe697",
            "kernel/sched/core.c": "7b8a90ae1b87eb43e53716a5ebc2b34555f630b1",
            "kernel/sched/ext.c": "65da2542d7be3dcbdda3fa3c359af9f4eaddf1f0",
            "kernel/sched/ext.h": "fffbfa23b9e2100cfa46a2ffe6a78b649e5fff79",
        },
        output_sha256={
            "include/linux/sched/hmbird.h": "4af71da721a1feb1d99f1f6d3e7d224dd628db334f63681fe2eb6e4a78176785",
            "include/linux/sched/hmbird_version.h": "ef32bbad31e880838760768e131814c5e469316e112c0e74dc10d63e4f8ad679",
            "kernel/sched/hmbird.h": "491d42dc95c73f4e4d96ea860abf4ef889b4571ac3fb61cca3a7b36182c8a4cb",
            "kernel/sched/hmbird/hmbird.c": "00110372a1739ca0d85134f6a0151aa8bd6b70e5a5be81b24d395136380be706",
            "kernel/sched/hmbird/hmbird_sched.h": "75dcef1027216bf6c01a764cb8508135563affd9f62059b803a70ec9b6a1a45d",
        },
    ),
    "oos15-global": BaseSpec(
        vendor_commit="59336d4db04efdc70e1c63d6a92f7e4d14efafa8",
        main_patch=Path("oneplus/hmbird/fengchi_OP13-CPH_A15.patch"),
        main_sha256="885f9cbfbe63dd57e2f681e17a8a1e4be7e18c37d8c48de1dab6a44db672199b",
        compatibility_patch=Path("patches/oneplus13/hmbird/vendor-preimage-oos15.patch"),
        compatibility_sha256="26ddc31a46979eda7954378245ef12a5fca9d973bfe7455b2db109f857699e81",
        preimage_blobs={
            "include/linux/sched.h": "b3629d39dd0a38ce61bccdb1728bc771b95b3139",
            "include/linux/sched/ext.h": "31c45aa285a5a82751dacdf92153c63d553fe697",
            "kernel/sched/core.c": "933efe061ae8c428f84392464514f6b28031a1cb",
            "kernel/sched/ext.c": "65da2542d7be3dcbdda3fa3c359af9f4eaddf1f0",
            "kernel/sched/ext.h": "fffbfa23b9e2100cfa46a2ffe6a78b649e5fff79",
        },
        output_sha256={
            "include/linux/sched/hmbird.h": "4af71da721a1feb1d99f1f6d3e7d224dd628db334f63681fe2eb6e4a78176785",
            "include/linux/sched/hmbird_version.h": "ef32bbad31e880838760768e131814c5e469316e112c0e74dc10d63e4f8ad679",
            "kernel/sched/hmbird.h": "491d42dc95c73f4e4d96ea860abf4ef889b4571ac3fb61cca3a7b36182c8a4cb",
            "kernel/sched/hmbird/hmbird.c": "00110372a1739ca0d85134f6a0151aa8bd6b70e5a5be81b24d395136380be706",
            "kernel/sched/hmbird/hmbird_sched.h": "75dcef1027216bf6c01a764cb8508135563affd9f62059b803a70ec9b6a1a45d",
        },
    ),
    "oos16": BaseSpec(
        vendor_commit="73ecb0dc41fb28ce5727465bd19d7469b4a6db73",
        main_patch=Path("oneplus/hmbird/fengchi_OP13_A16.patch"),
        main_sha256="b4c812e33f223ecef0c7e5f7c1c69d05c5f04fe8c810ca4baf551241ec4ffc8f",
        compatibility_patch=Path("patches/oneplus13/hmbird/vendor-preimage-oos16.patch"),
        compatibility_sha256="ad0533d964bb5731343faa41ea53dc8c4d7f878894304d0dc43f251f52ce2a1f",
        preimage_blobs={
            "include/linux/sched.h": "b3629d39dd0a38ce61bccdb1728bc771b95b3139",
            "include/linux/sched/ext.h": "31c45aa285a5a82751dacdf92153c63d553fe697",
            "kernel/sched/core.c": "43bc73c887a226a4d0518690e298d7c2099e374f",
            "kernel/sched/ext.c": "65da2542d7be3dcbdda3fa3c359af9f4eaddf1f0",
            "kernel/sched/ext.h": "fffbfa23b9e2100cfa46a2ffe6a78b649e5fff79",
            "kernel/time/tick-sched.c": "254624e4bd431980ece3413a15f1b01ee444187a",
        },
        output_sha256={
            "include/linux/sched/hmbird_version.h": "ef32bbad31e880838760768e131814c5e469316e112c0e74dc10d63e4f8ad679",
            "kernel/sched/hmbird.h": "906e2a940eeda0b2e34dec1db20dc2ae7d7a46b1bfc1b46fd51ecd3f3fc3e14f",
            "kernel/sched/hmbird/hmbird.c": "a2c3942d844d7fb27c9dddb25ff8931ff7cd7c324f659984080de2b02714bfae",
            "kernel/sched/hmbird/hmbird_sched.h": "a2f95df47a1463418f152b82ee80d159ecb2edc7bce9d5934f0bb62d5af0a7f1",
        },
    ),
}


class IntegrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class PatchChanges:
    targets: frozenset[Path]
    deletions: frozenset[Path]
    creations: frozenset[Path]


@dataclass(frozen=True)
class FileSnapshot:
    existed: bool
    payload: bytes | None
    mode: int | None


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
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


def _require_plain_directory(path: Path, label: str) -> Path:
    if path.is_symlink():
        raise IntegrationError(f"{label} directory is a symlink: {path}")
    resolved = path.resolve()
    if not resolved.is_dir():
        raise IntegrationError(f"{label} directory is missing: {resolved}")
    return resolved


def _require_plain_file(path: Path, label: str) -> Path:
    if path.is_symlink():
        raise IntegrationError(f"{label} file is a symlink: {path}")
    resolved = path.resolve()
    if not resolved.is_file():
        raise IntegrationError(f"{label} file is missing: {resolved}")
    return resolved


def _git_head(checkout: Path, label: str) -> str:
    if not (checkout / ".git").exists():
        raise IntegrationError(f"{label} checkout lacks Git metadata: {checkout}")
    result = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "--verify", "HEAD"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    head = result.stdout.strip()
    if (
        result.returncode != 0
        or len(head) != 40
        or any(character not in "0123456789abcdef" for character in head)
    ):
        detail = result.stderr.strip() or head
        raise IntegrationError(f"failed to resolve pinned {label} commit: {detail}")
    return head


def _git_blob_bytes(checkout: Path, relative: Path, label: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(checkout), "show", f"HEAD:{relative.as_posix()}"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise IntegrationError(f"failed to read pinned {label} blob {relative}: {detail}")
    return result.stdout


def _git_blob_oid(checkout: Path, relative: Path, label: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "--verify", f"HEAD:{relative.as_posix()}"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    oid = result.stdout.strip()
    if (
        result.returncode != 0
        or len(oid) != 40
        or any(character not in "0123456789abcdef" for character in oid)
    ):
        detail = result.stderr.strip() or oid
        raise IntegrationError(f"failed to resolve pinned {label} blob {relative}: {detail}")
    return oid


def _patch_utility() -> str:
    executable = shutil.which("patch")
    if executable:
        return executable
    git = shutil.which("git")
    if git:
        bundled = Path(git).resolve().parents[1] / "usr" / "bin" / "patch.exe"
        if bundled.is_file():
            return str(bundled)
    raise IntegrationError("GNU patch is required")


def _gnu_patch_version(executable: str) -> str:
    result = subprocess.run(
        [executable, "--version"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    first_line = result.stdout.splitlines()[0] if result.stdout else ""
    if result.returncode != 0 or "GNU patch" not in first_line:
        raise IntegrationError(f"unsupported patch implementation: {first_line!r}")
    return first_line


def _normalize_patch_path(raw: str, label: str) -> Path | None:
    value = raw.split("\t", 1)[0].strip()
    if value == "/dev/null":
        return None
    if "\\" in value:
        raise IntegrationError(f"{label} patch path contains a backslash: {value!r}")
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or len(candidate.parts) < 2 or candidate.parts[0] not in {"a", "b"}:
        raise IntegrationError(f"{label} patch path is not strip-one relative: {value!r}")
    relative = Path(*candidate.parts[1:])
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise IntegrationError(f"{label} patch path escapes its tree: {value!r}")
    return relative


def _patch_changes(payload: bytes, label: str) -> PatchChanges:
    try:
        lines = payload.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise IntegrationError(f"{label} patch is not UTF-8") from exc
    targets: set[Path] = set()
    deletions: set[Path] = set()
    creations: set[Path] = set()
    before: Path | None | object = _MISSING
    for line_number, line in enumerate(lines, start=1):
        if line.startswith("--- "):
            before = _normalize_patch_path(line[4:], f"{label} line {line_number}")
            continue
        if line.startswith("+++ ") and before is not _MISSING:
            after = _normalize_patch_path(line[4:], f"{label} line {line_number}")
            if before is None and after is None:
                raise IntegrationError(f"{label} patch has a null-to-null change at line {line_number}")
            if before is not None and after is not None and before != after:
                raise IntegrationError(f"{label} patch renames {before} to {after}")
            target = after if after is not None else before
            assert isinstance(target, Path)
            targets.add(target)
            if before is None:
                creations.add(target)
            if after is None:
                deletions.add(target)
            before = _MISSING
    if before is not _MISSING:
        raise IntegrationError(f"{label} patch has an unmatched old-file header")
    if not targets:
        raise IntegrationError(f"{label} patch has no file targets")
    return PatchChanges(
        targets=frozenset(targets),
        deletions=frozenset(deletions),
        creations=frozenset(creations),
    )


_MISSING = object()


def _residue(root: Path) -> set[Path]:
    return {
        path.relative_to(root)
        for suffix in ("*.rej", "*.orig")
        for path in root.rglob(suffix)
        if path.is_file()
    }


def _copy_targets(source: Path, destination: Path, targets: frozenset[Path]) -> None:
    for relative in sorted(targets, key=lambda item: item.as_posix()):
        raw_source = source / relative
        if raw_source.is_symlink():
            raise IntegrationError(f"patch target is a symlink: {relative.as_posix()}")
        if raw_source.exists() and not raw_source.is_file():
            raise IntegrationError(f"patch target is not a file: {relative.as_posix()}")
        if raw_source.is_file():
            output = destination / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(raw_source, output)


def _run_patch_payload(
    tree: Path,
    payload: bytes,
    filename: str,
    executable: str,
) -> tuple[int, str]:
    with tempfile.TemporaryDirectory(prefix="op13-hmbird-patch-") as temporary_name:
        patch_file = Path(temporary_name) / Path(filename).name
        patch_file.write_bytes(payload)
        command = [
            executable,
            "--batch",
            "--forward",
            "--fuzz=0",
            "--no-backup-if-mismatch",
            "--reject-file=-",
            "-p1",
            "--input",
            str(patch_file),
        ]
        result = subprocess.run(
            command,
            cwd=tree,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
    return result.returncode, result.stdout


def _apply_patch(
    tree: Path,
    payload: bytes,
    filename: str,
    label: str,
    executable: str,
) -> str:
    return_code, output = _run_patch_payload(tree, payload, filename, executable)
    rejected = re.search(
        r"\bFAILED\b|\bignored\b|saving rejects|Reversed \(or previously applied\)",
        output,
        re.IGNORECASE,
    )
    if return_code != 0 or rejected is not None:
        raise IntegrationError(
            f"{label} patch failed with exit {return_code}\n{output[-6000:]}"
        )
    return output


def _read_text(path: Path, label: str) -> str:
    plain = _require_plain_file(path, label)
    try:
        return plain.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise IntegrationError(f"{label} is not UTF-8: {plain}") from exc


def _assert_outputs(
    tree: Path,
    spec: BaseSpec,
    main_changes: PatchChanges,
) -> list[dict[str, str]]:
    residue = _residue(tree)
    if residue:
        raise IntegrationError(
            f"HMBIRD patch residue remains: {sorted(path.as_posix() for path in residue)}"
        )
    remaining_deletions = [
        relative.as_posix()
        for relative in sorted(main_changes.deletions, key=lambda item: item.as_posix())
        if (tree / relative).exists() or (tree / relative).is_symlink()
    ]
    if remaining_deletions:
        raise IntegrationError(f"declared SCX/deprecated deletions remain: {remaining_deletions}")
    for relative, tokens in FORBIDDEN_OUTPUT_TOKENS.items():
        text = _read_text(tree / relative, f"HMBIRD output {relative}")
        for token in tokens:
            if token in text:
                raise IntegrationError(f"HMBIRD output {relative} retains forbidden token {token!r}")
    for relative, tokens in REQUIRED_OUTPUT_TOKENS.items():
        text = _read_text(tree / relative, f"HMBIRD output {relative}")
        for token in tokens:
            if token not in text:
                raise IntegrationError(f"HMBIRD output {relative} lacks required token {token!r}")
    outputs: list[dict[str, str]] = []
    for relative, expected in sorted(spec.output_sha256.items()):
        path = _require_plain_file(tree / relative, f"HMBIRD output {relative}")
        actual = sha256_file(path)
        if actual != expected:
            raise IntegrationError(
                f"HMBIRD output {relative} changed: expected {expected}, got {actual}"
            )
        outputs.append({"path": relative, "sha256": actual})
    return outputs


def _snapshot_targets(
    tree: Path,
    targets: frozenset[Path],
) -> tuple[dict[Path, FileSnapshot], dict[Path, bool]]:
    snapshots: dict[Path, FileSnapshot] = {}
    directories: dict[Path, bool] = {}
    for relative in sorted(targets, key=lambda item: item.as_posix()):
        path = tree / relative
        if path.is_symlink():
            raise IntegrationError(f"patch target is a symlink: {relative.as_posix()}")
        if path.exists() and not path.is_file():
            raise IntegrationError(f"patch target is not a file: {relative.as_posix()}")
        if path.is_file():
            snapshots[relative] = FileSnapshot(
                existed=True,
                payload=path.read_bytes(),
                mode=stat.S_IMODE(path.stat().st_mode),
            )
        else:
            snapshots[relative] = FileSnapshot(existed=False, payload=None, mode=None)
        parent = path.parent
        while parent != tree:
            directories.setdefault(parent, parent.exists())
            parent = parent.parent
    return snapshots, directories


def _restore_targets(
    tree: Path,
    snapshots: Mapping[Path, FileSnapshot],
    directories: Mapping[Path, bool],
) -> None:
    for relative, snapshot in snapshots.items():
        path = tree / relative
        if snapshot.existed:
            if path.is_symlink() or (path.exists() and not path.is_file()):
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
            path.parent.mkdir(parents=True, exist_ok=True)
            assert snapshot.payload is not None and snapshot.mode is not None
            path.write_bytes(snapshot.payload)
            path.chmod(snapshot.mode)
        elif path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            shutil.rmtree(path)
    for path, existed in sorted(
        directories.items(),
        key=lambda item: len(item[0].parts),
        reverse=True,
    ):
        if not existed and path.is_dir():
            try:
                path.rmdir()
            except OSError:
                pass
    for relative in _residue(tree):
        (tree / relative).unlink(missing_ok=True)


def _assert_pinned_preimages(
    source: Path,
    spec: BaseSpec,
    compatibility_changes: PatchChanges,
) -> None:
    declared = {path.as_posix() for path in compatibility_changes.targets}
    expected = set(spec.preimage_blobs)
    if declared != expected:
        raise IntegrationError(
            "compatibility preimage declaration changed: "
            f"expected={sorted(expected)}, actual={sorted(declared)}"
        )
    for relative, expected_oid in sorted(spec.preimage_blobs.items()):
        actual_oid = _git_blob_oid(source, Path(relative), "vendor kernel")
        if actual_oid != expected_oid:
            raise IntegrationError(
                f"vendor preimage {relative} changed: expected {expected_oid}, got {actual_oid}"
            )
    for relative, expected_sha256 in sorted(SCX_WORKTREE_SHA256.items()):
        path = _require_plain_file(source / relative, f"vendor SCX preimage {relative}")
        actual_sha256 = sha256_file(path)
        if actual_sha256 != expected_sha256:
            raise IntegrationError(
                f"vendor SCX preimage {relative} changed: "
                f"expected {expected_sha256}, got {actual_sha256}"
            )


def integrate(
    source_dir: Path,
    wild_dir: Path,
    base: str,
    *,
    repository_root: Path | None = None,
    stamp: Path | None = None,
) -> dict[str, Any]:
    if base not in BASE_SPECS:
        raise IntegrationError(f"unsupported OnePlus base {base!r}")
    spec = BASE_SPECS[base]
    source = _require_plain_directory(source_dir, "vendor kernel")
    wild = _require_plain_directory(wild_dir, "Wild patch")
    root = (
        _require_plain_directory(repository_root, "builder repository")
        if repository_root is not None
        else Path(__file__).resolve().parents[1]
    )
    source_commit = _git_head(source, "vendor kernel")
    if source_commit != spec.vendor_commit:
        raise IntegrationError(
            f"vendor kernel commit changed: expected {spec.vendor_commit}, got {source_commit}"
        )
    wild_commit = _git_head(wild, "Wild patch")
    if wild_commit != EXPECTED_WILD_COMMIT:
        raise IntegrationError(
            f"Wild patch commit changed: expected {EXPECTED_WILD_COMMIT}, got {wild_commit}"
        )
    stamp_path = stamp.resolve() if stamp is not None else source / STAMP_NAME
    try:
        stamp_path.relative_to(source)
    except ValueError as exc:
        raise IntegrationError("integration stamp must stay inside the vendor kernel tree") from exc
    if stamp_path.exists() or stamp_path.is_symlink():
        raise IntegrationError(f"HMBIRD integration stamp already exists: {stamp_path}")
    existing_residue = _residue(source)
    if existing_residue:
        raise IntegrationError(
            "vendor kernel contains patch residue: "
            f"{sorted(path.as_posix() for path in existing_residue)}"
        )

    compatibility_path = _require_plain_file(
        root / spec.compatibility_patch,
        "vendor HMBIRD compatibility patch",
    )
    compatibility_payload = compatibility_path.read_bytes()
    compatibility_sha256 = sha256_bytes(compatibility_payload)
    if compatibility_sha256 != spec.compatibility_sha256:
        raise IntegrationError(
            "vendor HMBIRD compatibility patch changed: "
            f"expected {spec.compatibility_sha256}, got {compatibility_sha256}"
        )
    main_payload = _git_blob_bytes(wild, spec.main_patch, "Wild Fengchi")
    main_sha256 = sha256_bytes(main_payload)
    if main_sha256 != spec.main_sha256:
        raise IntegrationError(
            f"Wild Fengchi patch changed: expected {spec.main_sha256}, got {main_sha256}"
        )
    compatibility_changes = _patch_changes(compatibility_payload, "compatibility")
    main_changes = _patch_changes(main_payload, "Wild Fengchi")
    _assert_pinned_preimages(source, spec, compatibility_changes)

    all_targets = frozenset(compatibility_changes.targets | main_changes.targets)
    executable = _patch_utility()
    version = _gnu_patch_version(executable)
    with tempfile.TemporaryDirectory(prefix="op13-hmbird-preflight-") as temporary_name:
        sandbox = Path(temporary_name)
        _copy_targets(source, sandbox, all_targets)
        _apply_patch(
            sandbox,
            compatibility_payload,
            compatibility_path.name,
            "vendor HMBIRD compatibility preflight",
            executable,
        )
        _apply_patch(
            sandbox,
            main_payload,
            spec.main_patch.name,
            "Wild Fengchi preflight",
            executable,
        )
        _assert_outputs(sandbox, spec, main_changes)

    snapshots, directories = _snapshot_targets(source, all_targets)
    try:
        compatibility_output = _apply_patch(
            source,
            compatibility_payload,
            compatibility_path.name,
            "vendor HMBIRD compatibility",
            executable,
        )
        main_output = _apply_patch(
            source,
            main_payload,
            spec.main_patch.name,
            "Wild Fengchi",
            executable,
        )
        outputs = _assert_outputs(source, spec, main_changes)
        document: dict[str, Any] = {
            "schema_version": 1,
            "integration": "oneplus-vendor-hmbird-fengchi",
            "base": base,
            "inputs": {
                "vendor_commit": source_commit,
                "wild_commit": wild_commit,
                "main_patch": spec.main_patch.as_posix(),
                "main_patch_sha256": main_sha256,
                "compatibility_patch": spec.compatibility_patch.as_posix(),
                "compatibility_patch_sha256": compatibility_sha256,
            },
            "preimage_blobs": dict(sorted(spec.preimage_blobs.items())),
            "scx_worktree_sha256": dict(sorted(SCX_WORKTREE_SHA256.items())),
            "patch_tool": {
                "version": version,
                "fuzz": 0,
                "forward_only": True,
                "compatibility_output_sha256": sha256_bytes(
                    compatibility_output.encode("utf-8")
                ),
                "main_output_sha256": sha256_bytes(main_output.encode("utf-8")),
            },
            "declared_deletions": [
                path.as_posix()
                for path in sorted(main_changes.deletions, key=lambda item: item.as_posix())
            ],
            "outputs": outputs,
        }
        _atomic_json(stamp_path, document)
        return document
    except Exception:
        _restore_targets(source, snapshots, directories)
        stamp_path.unlink(missing_ok=True)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--wild-dir", required=True, type=Path)
    parser.add_argument("--base", required=True, choices=tuple(sorted(BASE_SPECS)))
    parser.add_argument("--repository-root", type=Path)
    parser.add_argument("--stamp", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        document = integrate(
            args.source_dir,
            args.wild_dir,
            args.base,
            repository_root=args.repository_root,
            stamp=args.stamp,
        )
    except IntegrationError as exc:
        print(f"integration error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(document, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
