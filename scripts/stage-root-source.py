#!/usr/bin/env python3
"""Stage an audited KernelSU driver tree from pinned Git objects."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any


EXPECTED_COMMITS = {
    "kernelsu": "b0bc817b4e966aa6aa830834eaf6ef765d821d40",
    "kernelsu-next": "1a0ef4898568a013b51d74ceb5593b83725bfb78",
}
EXPECTED_SOURCE_TREES = {
    "kernelsu": {
        "kernel": "fc8536239648895932e9471e10be48115b73c26f",
        "uapi": "050e07bb02b51fb02a8395545ac1cca18d8660ba",
    },
    "kernelsu-next": {
        "kernel": "4710db52bbdc5fb26390ee64bc0fb336e8489b50",
        "uapi": "fc98ae0140c80815260ecaf86ddb6ef06ad29863",
    },
}
EXPECTED_LINK_PATH = PurePosixPath("kernel/include/uapi")
EXPECTED_LINK_TARGET = b"../../uapi"
EXPECTED_LINK_BLOB = "8fd1b18bf2b769547013f9ca91126f49f513744c"
EXPECTED_UAPI_FILES = {
    "app_profile.h",
    "feature.h",
    "ksu.h",
    "selinux.h",
    "sulog.h",
    "supercall.h",
}
STAMP_NAME = ".op13-root-stage.json"
REGULAR_MODES = {"100644", "100755"}


class StageError(RuntimeError):
    pass


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _git(source: Path, *arguments: str, binary: bool = False) -> str | bytes:
    result = subprocess.run(
        ["git", "-C", str(source), *arguments],
        check=False,
        capture_output=True,
        text=not binary,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace") if binary else result.stderr
        raise StageError(f"git {' '.join(arguments)} failed: {stderr.strip()}")
    return result.stdout


def _require_plain_directory(path: Path, label: str) -> Path:
    if path.is_symlink():
        raise StageError(f"{label} must not be a symbolic link: {path}")
    resolved = path.resolve()
    if not resolved.is_dir():
        raise StageError(f"{label} is missing: {resolved}")
    return resolved


def _git_entries(source: Path) -> dict[str, tuple[str, str, bytes]]:
    raw = _git(source, "ls-tree", "-r", "-z", "HEAD", "--", "kernel", "uapi", binary=True)
    assert isinstance(raw, bytes)
    entries: dict[str, tuple[str, str, bytes]] = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, kind, object_id = metadata.decode("ascii").split()
            decoded_path = raw_path.decode("utf-8")
        except (UnicodeDecodeError, ValueError) as exc:
            raise StageError("root dependency Git tree contains an unsupported entry") from exc
        path = PurePosixPath(decoded_path)
        if (
            path.is_absolute()
            or path.as_posix() != decoded_path
            or any(part in {"", ".", ".."} for part in path.parts)
            or "\\" in decoded_path
            or any(ord(character) < 32 for character in decoded_path)
        ):
            raise StageError(f"unsafe root dependency path: {decoded_path!r}")
        if kind != "blob" or mode not in REGULAR_MODES | {"120000"}:
            raise StageError(f"unsupported root dependency entry: {decoded_path} ({mode} {kind})")
        blob = _git(source, "cat-file", "blob", object_id, binary=True)
        assert isinstance(blob, bytes)
        if decoded_path in entries:
            raise StageError(f"duplicate root dependency path: {decoded_path}")
        entries[decoded_path] = (mode, object_id, blob)
    return entries


def _output_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for current, directories, names in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        safe_directories: list[str] = []
        for name in sorted(directories):
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink() or not path.is_dir():
                raise StageError(f"unsafe staged root directory: {relative}")
            safe_directories.append(name)
        directories[:] = safe_directories
        for name in sorted(names):
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink() or not path.is_file():
                raise StageError(f"unsafe staged root file: {relative}")
            files.append(path)
    return sorted(files, key=lambda item: item.relative_to(root).as_posix())


def _tree_digest(root: Path, records: dict[str, tuple[str, bytes]]) -> str:
    digest = hashlib.sha256()
    for relative, (mode, expected) in sorted(records.items()):
        path = root.joinpath(*PurePosixPath(relative).parts)
        actual = path.read_bytes()
        if actual != expected:
            raise StageError(f"staged root blob changed while writing: {relative}")
        raw_relative = relative.encode("utf-8")
        digest.update(len(raw_relative).to_bytes(4, "big"))
        digest.update(raw_relative)
        digest.update(mode.encode("ascii"))
        digest.update(len(actual).to_bytes(8, "big"))
        digest.update(actual)
    return digest.hexdigest()


def stage(
    workspace: Path,
    source_root: Path,
    source: Path,
    destination: Path,
    variant: str,
) -> dict[str, Any]:
    expected_commit = EXPECTED_COMMITS.get(variant)
    if expected_commit is None:
        raise StageError(f"unsupported staged root variant: {variant}")
    workspace = _require_plain_directory(Path(workspace), "workspace")
    source_root = _require_plain_directory(Path(source_root), "root dependency cache")
    source = _require_plain_directory(Path(source), "root dependency checkout")
    if source == source_root or source.parent != source_root:
        raise StageError("root dependency checkout escapes its verified cache root")

    destination_raw = Path(destination)
    if destination_raw.is_symlink() or destination_raw.exists():
        raise StageError(f"staged root destination already exists: {destination_raw}")
    destination = destination_raw.resolve()
    if not _inside(destination, workspace):
        raise StageError("staged root destination escapes the workspace")
    if _inside(destination, source) or _inside(source, destination):
        raise StageError("source and staged root destination must not contain each other")

    if not (source / ".git").exists():
        raise StageError(f"root dependency checkout lacks Git metadata: {source}")
    commit = str(_git(source, "rev-parse", "HEAD")).strip()
    if commit != expected_commit:
        raise StageError(f"unexpected {variant} commit: {commit}")
    status = _git(
        source,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignored=matching",
        binary=True,
    )
    assert isinstance(status, bytes)
    if status:
        raise StageError(f"{variant} checkout is not byte-clean")

    source_trees = {
        name: str(_git(source, "rev-parse", f"HEAD:{name}")).strip()
        for name in ("kernel", "uapi")
    }
    if source_trees != EXPECTED_SOURCE_TREES[variant]:
        raise StageError(f"{variant} source tree contract changed: {source_trees}")

    entries = _git_entries(source)
    links = {
        path: (object_id, blob)
        for path, (mode, object_id, blob) in entries.items()
        if mode == "120000"
    }
    expected_links = {
        EXPECTED_LINK_PATH.as_posix(): (EXPECTED_LINK_BLOB, EXPECTED_LINK_TARGET)
    }
    if links != expected_links:
        raise StageError(f"{variant} kernel symlink contract changed: {sorted(links)}")

    uapi_paths = {
        path.removeprefix("uapi/")
        for path, (mode, _object_id, _blob) in entries.items()
        if path.startswith("uapi/") and mode in REGULAR_MODES
    }
    if uapi_paths != EXPECTED_UAPI_FILES:
        raise StageError(
            f"{variant} UAPI inventory changed: expected={sorted(EXPECTED_UAPI_FILES)}, "
            f"actual={sorted(uapi_paths)}"
        )

    records: dict[str, tuple[str, bytes]] = {}
    for source_path, (mode, _object_id, blob) in entries.items():
        if source_path == EXPECTED_LINK_PATH.as_posix():
            continue
        if mode not in REGULAR_MODES:
            raise StageError(f"unexpected root dependency link: {source_path}")
        if source_path.startswith("kernel/"):
            destination_path = source_path
        elif source_path.startswith("uapi/"):
            destination_path = f"kernel/include/uapi/{source_path.removeprefix('uapi/')}"
        else:
            raise StageError(f"unexpected root dependency path: {source_path}")
        if destination_path in records:
            raise StageError(f"materialized root path collides: {destination_path}")
        records[destination_path] = (mode, blob)
    for required in ("kernel/Kconfig", "kernel/Kbuild"):
        if required not in records:
            raise StageError(f"required root driver file is missing: {required}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.tmp-", dir=destination.parent))
    try:
        for relative, (mode, blob) in sorted(records.items()):
            path = temporary.joinpath(*PurePosixPath(relative).parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as handle:
                handle.write(blob)
            path.chmod(0o755 if mode == "100755" else 0o644)
        output_paths = {path.relative_to(temporary).as_posix() for path in _output_files(temporary)}
        if output_paths != set(records):
            raise StageError("staged root output inventory changed while writing")
        document: dict[str, Any] = {
            "schema_version": 1,
            "variant": variant,
            "source_commit": commit,
            "source_trees": source_trees,
            "source": "pinned-git-objects",
            "materialized_link": {
                "path": EXPECTED_LINK_PATH.as_posix(),
                "target": EXPECTED_LINK_TARGET.decode("ascii"),
                "blob": EXPECTED_LINK_BLOB,
                "source": "uapi",
            },
            "file_count": len(records),
            "tree_sha256": _tree_digest(temporary, records),
        }
        (temporary / STAMP_NAME).write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        temporary.replace(destination)
        return document
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--variant", choices=tuple(sorted(EXPECTED_COMMITS)), required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        document = stage(
            args.workspace,
            args.source_root,
            args.source,
            args.destination,
            args.variant,
        )
    except StageError as exc:
        print(f"stage error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(document, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
