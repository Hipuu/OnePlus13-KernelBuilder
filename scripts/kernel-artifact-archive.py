#!/usr/bin/env python3
"""Create or restore a sealed reusable kernel-build archive.

The external manifest is deliberately strict and canonical.  The tar payload
contains only regular files, directories, and validated in-tree leaf symlinks.
Restoration never delegates path handling to tarfile extraction helpers.
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
import tarfile
import tempfile
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


FORMAT_NAME = "oneplus13-kernel-build-archive"
FORMAT_VERSION = 2
EXCLUDED_PREFIXES = (
    "modules",
    ".op13/config-work",
    ".op13/config-work-msm-kernel",
)
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
WINDOWS_UNSAFE = frozenset('<>:"\\|?*')
WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)
MAX_MANIFEST = 64 * 1024 * 1024
MAX_ARCHIVE = 16 * 1024 * 1024 * 1024
MAX_TAR = 32 * 1024 * 1024 * 1024
MAX_MEMBER_COUNT = 200_000
# A USTAR size field holds eleven octal digits.  Exactly 8 GiB overflows it.
MAX_MEMBER_SIZE = (8 * 1024 * 1024 * 1024) - 1
MAX_TOTAL_FILE_BYTES = 24 * 1024 * 1024 * 1024
RESTORE_FREE_SPACE_RESERVE = 2 * 1024 * 1024 * 1024
FILESYSTEM_ALLOCATION_UNIT = 4096
ZSTD_TIMEOUT_SECONDS = 30 * 60
PROCESS_KILL_TIMEOUT_SECONDS = 10
COPY_CHUNK = 1024 * 1024
WINDOWS_REPARSE_ATTRIBUTE = 0x400
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


class ArchiveError(RuntimeError):
    """A fail-closed archive validation error."""


@dataclass(frozen=True)
class SourceEntry:
    path: str
    kind: str
    mode: int
    fs_size: int
    dev: int
    ino: int
    nlink: int
    mtime_ns: int
    ctime_ns: int
    target: str


class _HashingWriter:
    def __init__(self, raw: BinaryIO) -> None:
        self.raw = raw
        self.digest = hashlib.sha256()
        self.size = 0

    def write(self, data: bytes) -> int:
        written = self.raw.write(data)
        if written is None:
            written = len(data)
        if written != len(data):
            raise ArchiveError("short write while creating tar payload")
        self.digest.update(data)
        self.size += written
        return written

    def flush(self) -> None:
        self.raw.flush()

    def hexdigest(self) -> str:
        return self.digest.hexdigest()


class _ProcessDeadline:
    """Kill an external codec if it exceeds the fixed archive deadline."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self.process = process
        self.expired = threading.Event()
        self.timer = threading.Timer(ZSTD_TIMEOUT_SECONDS, self._expire)
        self.timer.daemon = True

    def _expire(self) -> None:
        if self.process.poll() is None:
            self.expired.set()
            try:
                self.process.kill()
            except OSError:
                pass

    def __enter__(self) -> _ProcessDeadline:
        self.timer.start()
        return self

    def __exit__(self, *unused: object) -> None:
        self.timer.cancel()

    def raise_if_expired(self) -> None:
        if self.expired.is_set():
            raise ArchiveError(
                f"zstd exceeded the {ZSTD_TIMEOUT_SECONDS}-second time limit"
            )


class _SealedSourceReader:
    def __init__(self, source_dir: Path, entry: SourceEntry) -> None:
        self.entry = entry
        path = source_dir.joinpath(*entry.path.split("/"))
        flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(path, flags)
            self.raw = os.fdopen(descriptor, "rb")
        except OSError as exc:
            raise ArchiveError(f"cannot open source file {entry.path}: {exc}") from exc
        before = os.fstat(self.raw.fileno())
        if not _metadata_matches(entry, before):
            self.raw.close()
            raise ArchiveError(f"source file changed before read: {entry.path}")
        self.digest = hashlib.sha256()
        self.total = 0

    def read(self, size: int = -1) -> bytes:
        remaining = self.entry.fs_size - self.total
        if size < 0 or size > remaining:
            size = remaining
        chunk = self.raw.read(size)
        self.total += len(chunk)
        if self.total > self.entry.fs_size:
            raise ArchiveError(f"source file grew during read: {self.entry.path}")
        self.digest.update(chunk)
        return chunk

    def finish(self) -> str:
        if self.total != self.entry.fs_size:
            raise ArchiveError(f"source file shrank during read: {self.entry.path}")
        if self.raw.read(1):
            raise ArchiveError(f"source file grew during read: {self.entry.path}")
        after = os.fstat(self.raw.fileno())
        if not _metadata_matches(self.entry, after):
            raise ArchiveError(f"source file changed during read: {self.entry.path}")
        return self.digest.hexdigest()

    def close(self) -> None:
        self.raw.close()


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _round_up(value: int, alignment: int) -> int:
    return ((value + alignment - 1) // alignment) * alignment


def _expected_tar_size(members: list[dict[str, object]]) -> int:
    payload_end = 0
    for member in members:
        size = member["size"]
        assert isinstance(size, int)
        payload_end += tarfile.BLOCKSIZE
        if member["type"] == "file":
            payload_end += _round_up(size, tarfile.BLOCKSIZE)
    return _round_up(payload_end + (2 * tarfile.BLOCKSIZE), tarfile.RECORDSIZE)


def _canonical_json_bytes(document: dict[str, object]) -> bytes:
    return (
        json.dumps(
            document,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("ascii")


def _reject_json_constant(value: str) -> object:
    raise ArchiveError(f"non-finite JSON value is forbidden: {value}")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ArchiveError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _lexists(path: Path) -> bool:
    return os.path.lexists(os.fspath(path))


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _is_windows_reparse(metadata: os.stat_result) -> bool:
    return bool(
        getattr(metadata, "st_reparse_tag", 0)
        or (getattr(metadata, "st_file_attributes", 0) & WINDOWS_REPARSE_ATTRIBUTE)
    )


def _path_is_junction(path: Path) -> bool:
    predicate = getattr(path, "is_junction", None)
    if predicate is None:
        return False
    try:
        return bool(predicate())
    except OSError as exc:
        raise ArchiveError(f"cannot inspect possible junction {path}: {exc}") from exc


def _reject_reparse(path: Path, metadata: os.stat_result, label: str) -> None:
    if _is_windows_reparse(metadata) or _path_is_junction(path):
        raise ArchiveError(f"{label} must not be a junction or reparse point: {path}")


def _existing_path_chain(path: Path) -> list[Path]:
    absolute = _absolute(path)
    chain: list[Path] = []
    current = absolute
    while True:
        chain.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent
    chain.reverse()
    return chain


def _require_plain_path_chain(path: Path, label: str) -> None:
    for component in _existing_path_chain(path):
        if not _lexists(component):
            raise ArchiveError(f"{label} path component does not exist: {component}")
        try:
            metadata = component.lstat()
        except OSError as exc:
            raise ArchiveError(
                f"{label} path component is not accessible: {component}: {exc}"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise ArchiveError(f"{label} must not traverse a symbolic link: {component}")
        _reject_reparse(component, metadata, label)


def _require_plain_directory(path: Path, label: str) -> os.stat_result:
    _require_plain_path_chain(path, label)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ArchiveError(f"{label} is not accessible: {path}: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ArchiveError(f"{label} must be a plain directory: {path}")
    _reject_reparse(path, metadata, label)
    return metadata


def _require_output_parent(path: Path, label: str) -> None:
    _require_plain_directory(path.parent, f"{label} parent")


def _is_within(candidate: Path, parent: Path) -> bool:
    try:
        candidate.resolve(strict=False).relative_to(parent.resolve(strict=True))
    except (OSError, ValueError):
        return False
    return True


def _validate_member_path(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ArchiveError("member path must be a non-empty string")
    if unicodedata.normalize("NFC", value) != value:
        raise ArchiveError(f"member path is not NFC-normalized: {value!r}")
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeError as exc:
        raise ArchiveError(f"member path is not valid UTF-8: {value!r}") from exc
    if len(encoded) > 4096:
        raise ArchiveError(f"member path is too long: {value!r}")
    if value.startswith("/") or value.endswith("/") or "\\" in value:
        raise ArchiveError(f"member path is not normalized POSIX: {value!r}")
    segments = value.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise ArchiveError(f"member path is not normalized POSIX: {value!r}")
    for segment in segments:
        segment_bytes = segment.encode("utf-8")
        if len(segment_bytes) > 255:
            raise ArchiveError(f"member path segment is too long: {value!r}")
        if segment.endswith((" ", ".")):
            raise ArchiveError(f"member path has an unsafe suffix: {value!r}")
        if any(character in WINDOWS_UNSAFE for character in segment):
            raise ArchiveError(f"member path has an unsafe character: {value!r}")
        if any(
            unicodedata.category(character) in {"Cc", "Cf", "Cs"}
            for character in segment
        ):
            raise ArchiveError(f"member path has a control character: {value!r}")
        stem = segment.split(".", 1)[0].upper()
        if stem in WINDOWS_RESERVED:
            raise ArchiveError(f"member path uses a reserved name: {value!r}")
    # USTAR avoids hidden PAX/GNU extension records.  It supports a 100-byte
    # name or a 155-byte prefix plus a 100-byte final component.
    probe = tarfile.TarInfo(value)
    probe.mode = 0o600
    probe.uid = probe.gid = probe.mtime = probe.size = 0
    probe.uname = probe.gname = ""
    try:
        probe.tobuf(tarfile.USTAR_FORMAT, "utf-8", "strict")
    except (UnicodeError, ValueError, OverflowError) as exc:
        raise ArchiveError(f"member path is not representable in USTAR: {value!r}") from exc
    return value


def _validate_symlink_target(link_path: str, value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ArchiveError(f"symbolic link target must be non-empty: {link_path}")
    if unicodedata.normalize("NFC", value) != value:
        raise ArchiveError(f"symbolic link target is not NFC-normalized: {link_path}")
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeError as exc:
        raise ArchiveError(f"symbolic link target is not valid UTF-8: {link_path}") from exc
    if len(encoded) > 100:
        raise ArchiveError(f"symbolic link target is not representable in USTAR: {link_path}")
    if value.startswith("/") or value.endswith("/") or "\\" in value:
        raise ArchiveError(f"symbolic link target must be a normalized relative path: {link_path}")
    segments = value.split("/")
    if any(segment in {"", "."} for segment in segments):
        raise ArchiveError(f"symbolic link target must be a normalized relative path: {link_path}")
    saw_named_segment = False
    for segment in segments:
        if segment == "..":
            if saw_named_segment:
                raise ArchiveError(
                    f"symbolic link target must be a normalized relative path: {link_path}"
                )
            continue
        saw_named_segment = True
        if segment.endswith((" ", ".")):
            raise ArchiveError(f"symbolic link target has an unsafe suffix: {link_path}")
        if any(character in WINDOWS_UNSAFE for character in segment):
            raise ArchiveError(f"symbolic link target has an unsafe character: {link_path}")
        if any(
            unicodedata.category(character) in {"Cc", "Cf", "Cs"}
            for character in segment
        ):
            raise ArchiveError(f"symbolic link target has a control character: {link_path}")
        if segment.split(".", 1)[0].upper() in WINDOWS_RESERVED:
            raise ArchiveError(f"symbolic link target uses a reserved name: {link_path}")
    # The USTAR linkname field has no prefix extension.  Generating the exact
    # prospective header catches all encoding and field-width edge cases.
    probe = tarfile.TarInfo(link_path)
    probe.type = tarfile.SYMTYPE
    probe.mode = 0o777
    probe.uid = probe.gid = probe.mtime = probe.size = 0
    probe.uname = probe.gname = ""
    probe.linkname = value
    try:
        probe.tobuf(tarfile.USTAR_FORMAT, "utf-8", "strict")
    except (UnicodeError, ValueError, OverflowError) as exc:
        raise ArchiveError(
            f"symbolic link target is not representable in USTAR: {link_path}"
        ) from exc
    return value


def _lexical_link_destination(link_path: str, target: str) -> str:
    parts = link_path.split("/")[:-1]
    for segment in target.split("/"):
        if segment == "..":
            if not parts:
                raise ArchiveError(f"symbolic link escapes the archive root: {link_path}")
            parts.pop()
        else:
            parts.append(segment)
    if not parts:
        raise ArchiveError(f"symbolic link resolves to the archive root: {link_path}")
    return "/".join(parts)


def _validate_symlink_graph(members: list[dict[str, object]]) -> dict[str, str]:
    by_path = {str(member["path"]): member for member in members}
    final_kinds: dict[str, str] = {}
    # Cache complete virtual-path resolutions.  A long link chain is therefore
    # walked once instead of once for every link, while the iterative walk
    # avoids Python recursion limits for large but valid kernel kits.
    resolved: dict[str, tuple[str, str]] = {}

    def resolve_virtual(start: str, originating_link: str) -> tuple[str, str]:
        current = start
        trail: list[str] = []
        active_symlinks = {originating_link}
        while True:
            cached = resolved.get(current)
            if cached is not None:
                terminal = cached
                break
            trail.append(current)
            parts = current.split("/")
            redirected: str | None = None
            for index in range(len(parts)):
                prefix = "/".join(parts[: index + 1])
                member = by_path.get(prefix)
                if member is None:
                    raise ArchiveError(
                        f"symbolic link target is missing from archive: {current}"
                    )
                kind = member["type"]
                if kind == "symlink":
                    if prefix in active_symlinks:
                        raise ArchiveError(f"symbolic link cycle is forbidden: {prefix}")
                    active_symlinks.add(prefix)
                    target = member["target"]
                    assert isinstance(target, str)
                    redirected = _lexical_link_destination(prefix, target)
                    remainder = parts[index + 1 :]
                    if remainder:
                        redirected = "/".join((redirected, *remainder))
                    break
                if index != len(parts) - 1 and kind != "directory":
                    raise ArchiveError(
                        f"symbolic link traverses a non-directory: {current}"
                    )
            if redirected is not None:
                current = redirected
                continue
            final_kind = by_path[current]["type"]
            assert isinstance(final_kind, str)
            terminal = (current, final_kind)
            break
        for visited in trail:
            resolved[visited] = terminal
        return terminal

    for member in members:
        if member["type"] != "symlink":
            continue
        path = str(member["path"])
        target = member["target"]
        assert isinstance(target, str)
        destination = _lexical_link_destination(path, target)
        _, final_kind = resolve_virtual(destination, path)
        if final_kind not in {"file", "directory"}:
            raise ArchiveError(f"symbolic link has no regular final target: {path}")
        final_kinds[path] = final_kind
    return final_kinds


def _excluded(path: str) -> bool:
    return any(path == root or path.startswith(root + "/") for root in EXCLUDED_PREFIXES)


def _entry_from_stat(
    path: str,
    filesystem_path: Path,
    metadata: os.stat_result,
) -> SourceEntry:
    _reject_reparse(filesystem_path, metadata, f"source member {path}")
    mode = stat.S_IMODE(metadata.st_mode)
    if mode & ~0o777:
        raise ArchiveError(f"special permission bits are forbidden: {path}")
    target = ""
    if stat.S_ISDIR(metadata.st_mode):
        kind = "directory"
    elif stat.S_ISREG(metadata.st_mode):
        kind = "file"
        if metadata.st_nlink != 1:
            raise ArchiveError(f"hard-linked source file is forbidden: {path}")
    elif stat.S_ISLNK(metadata.st_mode):
        # Windows symlinks are reparse points and are rejected above.  Kernel
        # artifacts are produced on Linux, where relative leaf symlinks can be
        # represented and restored without traversing them during collection.
        kind = "symlink"
        try:
            target = _validate_symlink_target(path, os.readlink(filesystem_path))
        except OSError as exc:
            raise ArchiveError(f"cannot read symbolic link {path}: {exc}") from exc
        mode = 0o777
    else:
        raise ArchiveError(f"special filesystem entry is forbidden: {path}")
    return SourceEntry(
        path=path,
        kind=kind,
        mode=mode,
        fs_size=metadata.st_size,
        dev=metadata.st_dev,
        ino=metadata.st_ino,
        nlink=metadata.st_nlink,
        mtime_ns=metadata.st_mtime_ns,
        # Windows may report creation/change time with different sub-second
        # rounding through lstat() and fstat().  Size, mtime, identity, mode,
        # and link count remain stable race checks there.
        ctime_ns=0 if os.name == "nt" else metadata.st_ctime_ns,
        target=target,
    )


def _scan_source(source_dir: Path) -> list[SourceEntry]:
    _require_plain_directory(source_dir, "source directory")
    entries: list[SourceEntry] = []
    total_file_bytes = 0

    def visit(directory: Path, relative_parts: tuple[str, ...]) -> None:
        nonlocal total_file_bytes
        relative_label = "/".join(relative_parts) or "."
        _require_plain_directory(directory, f"source directory {relative_label}")
        try:
            with os.scandir(directory) as iterator:
                children = sorted(iterator, key=lambda item: item.name)
        except OSError as exc:
            relative = "/".join(relative_parts) or "."
            raise ArchiveError(f"cannot scan source directory {relative}: {exc}") from exc
        for child in children:
            relative = "/".join((*relative_parts, child.name))
            _validate_member_path(relative)
            try:
                # DirEntry.stat() reports st_nlink=0 on some Windows/Python
                # combinations.  os.lstat() preserves the hard-link count
                # required by this archive contract.
                metadata = os.lstat(child.path)
            except OSError as exc:
                raise ArchiveError(f"cannot stat source member {relative}: {exc}") from exc
            _reject_reparse(Path(child.path), metadata, f"source member {relative}")
            if relative in EXCLUDED_PREFIXES:
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                    raise ArchiveError(
                        f"excluded source root must be a plain directory: {relative}"
                    )
                continue
            entry = _entry_from_stat(relative, Path(child.path), metadata)
            if len(entries) >= MAX_MEMBER_COUNT:
                raise ArchiveError(f"source member count exceeds {MAX_MEMBER_COUNT}")
            if entry.kind == "file":
                if entry.fs_size > MAX_MEMBER_SIZE:
                    raise ArchiveError(f"source member exceeds the size limit: {entry.path}")
                total_file_bytes += entry.fs_size
                if total_file_bytes > MAX_TOTAL_FILE_BYTES:
                    raise ArchiveError("source file bytes exceed the aggregate size limit")
            entries.append(entry)
            if entry.kind == "directory":
                visit(Path(child.path), (*relative_parts, child.name))

    visit(source_dir, ())
    entries.sort(key=lambda item: item.path)
    folded: dict[str, str] = {}
    kinds = {entry.path: entry.kind for entry in entries}
    for entry in entries:
        portable = entry.path.casefold()
        previous = folded.setdefault(portable, entry.path)
        if previous != entry.path:
            raise ArchiveError(
                f"case-insensitive member collision: {previous!r} and {entry.path!r}"
            )
        parent = entry.path.rpartition("/")[0]
        if parent and kinds.get(parent) != "directory":
            raise ArchiveError(f"member parent is not a recorded directory: {entry.path}")
    graph_members = [
        {
            "mode": entry.mode,
            "path": entry.path,
            "sha256": EMPTY_SHA256,
            "size": 0 if entry.kind != "file" else entry.fs_size,
            "target": entry.target,
            "type": entry.kind,
        }
        for entry in entries
    ]
    _validate_symlink_graph(graph_members)
    if _expected_tar_size(graph_members) > MAX_TAR:
        raise ArchiveError("source tar payload exceeds the size limit")
    return entries


def _metadata_matches(entry: SourceEntry, metadata: os.stat_result) -> bool:
    expected_type = {
        "file": stat.S_ISREG,
        "directory": stat.S_ISDIR,
        "symlink": stat.S_ISLNK,
    }[entry.kind]
    return (
        expected_type(metadata.st_mode)
        and (
            entry.kind == "symlink"
            or stat.S_IMODE(metadata.st_mode) == entry.mode
        )
        and metadata.st_size == entry.fs_size
        and metadata.st_dev == entry.dev
        and metadata.st_ino == entry.ino
        and metadata.st_nlink == entry.nlink
        and metadata.st_mtime_ns == entry.mtime_ns
        and (entry.ctime_ns == 0 or metadata.st_ctime_ns == entry.ctime_ns)
    )


def _tar_info(entry: SourceEntry) -> tarfile.TarInfo:
    info = tarfile.TarInfo(entry.path)
    info.type = {
        "directory": tarfile.DIRTYPE,
        "file": tarfile.REGTYPE,
        "symlink": tarfile.SYMTYPE,
    }[entry.kind]
    info.mode = entry.mode
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    info.size = entry.fs_size if entry.kind == "file" else 0
    info.linkname = entry.target
    info.pax_headers = {}
    return info


def _write_tar(source_dir: Path, output: _HashingWriter) -> list[dict[str, object]]:
    before = _scan_source(source_dir)
    members: list[dict[str, object]] = []
    try:
        with tarfile.open(
            fileobj=output,
            mode="w|",
            format=tarfile.USTAR_FORMAT,
            encoding="utf-8",
            errors="strict",
        ) as archive:
            for entry in before:
                info = _tar_info(entry)
                if entry.kind in {"directory", "symlink"}:
                    archive.addfile(info)
                    digest = EMPTY_SHA256
                    size = 0
                else:
                    source = _SealedSourceReader(source_dir, entry)
                    try:
                        archive.addfile(info, source)
                        digest = source.finish()
                    finally:
                        source.close()
                    size = entry.fs_size
                members.append(
                    {
                        "mode": entry.mode,
                        "path": entry.path,
                        "sha256": digest,
                        "size": size,
                        "target": entry.target,
                        "type": entry.kind,
                    }
                )
    except (OSError, tarfile.TarError, UnicodeError, ValueError, OverflowError) as exc:
        raise ArchiveError(f"cannot create deterministic tar payload: {exc}") from exc
    expected_tar_size = _expected_tar_size(members)
    if output.size != expected_tar_size:
        raise ArchiveError("tar writer did not emit the exact canonical USTAR size")
    if output.size > MAX_TAR:
        raise ArchiveError("tar payload exceeds the size limit")
    after = _scan_source(source_dir)
    if before != after:
        raise ArchiveError("source tree changed while the archive was created")
    return members


def _stderr_text(stream: BinaryIO) -> str:
    stream.seek(0)
    data = stream.read(16 * 1024)
    return data.decode("utf-8", "replace").strip()


def _kill_and_wait(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=PROCESS_KILL_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise ArchiveError("zstd did not exit after it was killed") from exc


def _read_frame_bytes(stream: BinaryIO, size: int, label: str) -> bytes:
    value = stream.read(size)
    if len(value) != size:
        raise ArchiveError(f"zstd frame ended inside {label}")
    return value


def _validate_single_zstd_frame(
    stream: BinaryIO,
    expected_archive_size: int,
    expected_tar_size: int,
) -> None:
    """Reject concatenated, skippable, or trailing data before decompression."""

    try:
        stream.seek(0)
        if _read_frame_bytes(stream, 4, "magic") != ZSTD_MAGIC:
            raise ArchiveError("archive is not one standard zstd frame")
        descriptor = _read_frame_bytes(stream, 1, "frame descriptor")[0]
        fcs_flag = descriptor >> 6
        single_segment = bool(descriptor & 0x20)
        if descriptor & 0x18:
            raise ArchiveError("zstd frame uses reserved descriptor bits")
        has_checksum = bool(descriptor & 0x04)
        dictionary_flag = descriptor & 0x03
        if not single_segment:
            _read_frame_bytes(stream, 1, "window descriptor")
        dictionary_size = (0, 1, 2, 4)[dictionary_flag]
        if dictionary_size:
            dictionary_id = int.from_bytes(
                _read_frame_bytes(stream, dictionary_size, "dictionary id"),
                "little",
            )
            if dictionary_id != 0:
                raise ArchiveError("zstd dictionaries are forbidden")
        content_size_width = (1 if single_segment else 0, 2, 4, 8)[fcs_flag]
        if content_size_width:
            declared_content_size = int.from_bytes(
                _read_frame_bytes(stream, content_size_width, "content size"),
                "little",
            )
            if content_size_width == 2:
                declared_content_size += 256
            if declared_content_size != expected_tar_size:
                raise ArchiveError("zstd frame content size disagrees with the manifest")
        while True:
            header = int.from_bytes(
                _read_frame_bytes(stream, 3, "block header"), "little"
            )
            last_block = bool(header & 1)
            block_type = (header >> 1) & 0x3
            block_size = header >> 3
            if block_size > 128 * 1024:
                raise ArchiveError("zstd block exceeds the format size limit")
            if block_type == 3:
                raise ArchiveError("zstd frame contains a reserved block type")
            stored_size = 1 if block_type == 1 else block_size
            position = stream.tell()
            if position + stored_size > expected_archive_size:
                raise ArchiveError("zstd frame block exceeds the archive boundary")
            stream.seek(stored_size, os.SEEK_CUR)
            if last_block:
                break
        if has_checksum:
            _read_frame_bytes(stream, 4, "content checksum")
        if stream.tell() != expected_archive_size:
            raise ArchiveError(
                "zstd archive has a concatenated, skippable, or trailing payload"
            )
    except ArchiveError:
        raise
    except OSError as exc:
        raise ArchiveError(f"cannot validate zstd frame boundary: {exc}") from exc


def _write_archive_payload(
    source_dir: Path,
    temporary_archive: Path,
    compression: str,
    zstd_executable: str,
) -> tuple[list[dict[str, object]], int, str]:
    if compression == "none":
        try:
            with temporary_archive.open("xb") as raw:
                output = _HashingWriter(raw)
                members = _write_tar(source_dir, output)
                output.flush()
                os.fsync(raw.fileno())
        except OSError as exc:
            raise ArchiveError(f"cannot write temporary archive: {exc}") from exc
        return members, output.size, output.hexdigest()

    with temporary_archive.open("xb") as compressed, tempfile.TemporaryFile() as errors:
        try:
            process = subprocess.Popen(
                [zstd_executable, "-q", "-3", "-T1", "-c"],
                stdin=subprocess.PIPE,
                stdout=compressed,
                stderr=errors,
                shell=False,
            )
        except OSError as exc:
            raise ArchiveError(f"cannot start zstd compressor: {exc}") from exc
        assert process.stdin is not None
        output = _HashingWriter(process.stdin)
        with _ProcessDeadline(process) as deadline:
            try:
                members = _write_tar(source_dir, output)
                output.flush()
                process.stdin.close()
                result = process.wait()
                deadline.raise_if_expired()
            except BaseException:
                try:
                    process.stdin.close()
                except OSError:
                    pass
                _kill_and_wait(process)
                deadline.raise_if_expired()
                raise
        if result != 0:
            detail = _stderr_text(errors)
            suffix = f": {detail}" if detail else ""
            raise ArchiveError(f"zstd compressor exited with status {result}{suffix}")
        compressed.flush()
        os.fsync(compressed.fileno())
        return members, output.size, output.hexdigest()


def _hash_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(COPY_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
                total += len(chunk)
    except OSError as exc:
        raise ArchiveError(f"cannot hash archive: {exc}") from exc
    return total, digest.hexdigest()


def _compression_for_path(path: Path) -> str:
    name = path.name
    if name.endswith(".tar.zst"):
        return "zstd"
    if name.endswith(".tar"):
        return "none"
    raise ArchiveError("archive path must end in .tar or .tar.zst")


def _new_temporary_path(parent: Path, prefix: str) -> Path:
    descriptor, name = tempfile.mkstemp(dir=parent, prefix=prefix)
    os.close(descriptor)
    path = Path(name)
    path.unlink()
    return path


def create_archive(
    source_dir: Path,
    archive_path: Path,
    manifest_path: Path,
    *,
    zstd_executable: str = "zstd",
) -> dict[str, object]:
    source_dir = _absolute(Path(source_dir))
    archive_path = _absolute(Path(archive_path))
    manifest_path = _absolute(Path(manifest_path))
    _require_plain_directory(source_dir, "source directory")
    _require_output_parent(archive_path, "archive")
    _require_output_parent(manifest_path, "manifest")
    if archive_path == manifest_path:
        raise ArchiveError("archive and manifest paths must differ")
    if _is_within(archive_path, source_dir) or _is_within(manifest_path, source_dir):
        raise ArchiveError("archive and manifest must be outside the source directory")
    if _lexists(archive_path) or _lexists(manifest_path):
        raise ArchiveError("archive and manifest destinations must not already exist")
    if not isinstance(zstd_executable, str) or not zstd_executable:
        raise ArchiveError("zstd executable must be a non-empty argv element")
    compression = _compression_for_path(archive_path)
    temporary_archive = _new_temporary_path(
        archive_path.parent, f".{archive_path.name}.tmp-"
    )
    temporary_manifest = _new_temporary_path(
        manifest_path.parent, f".{manifest_path.name}.tmp-"
    )
    archive_installed = False
    try:
        members, tar_size, tar_sha256 = _write_archive_payload(
            source_dir,
            temporary_archive,
            compression,
            zstd_executable,
        )
        archive_size, archive_sha256 = _hash_file(temporary_archive)
        if archive_size > MAX_ARCHIVE:
            raise ArchiveError("archive exceeds the size limit")
        if compression == "zstd":
            try:
                with temporary_archive.open("rb") as compressed:
                    _validate_single_zstd_frame(compressed, archive_size, tar_size)
            except OSError as exc:
                raise ArchiveError(f"cannot validate created zstd archive: {exc}") from exc
        document: dict[str, object] = {
            "archive": {
                "compression": compression,
                "sha256": archive_sha256,
                "size": archive_size,
                "tar_sha256": tar_sha256,
                "tar_size": tar_size,
            },
            "exclusions": list(EXCLUDED_PREFIXES),
            "format": FORMAT_NAME,
            "members": members,
            "version": FORMAT_VERSION,
        }
        _validate_manifest_document(document)
        manifest_bytes = _canonical_json_bytes(document)
        if len(manifest_bytes) > MAX_MANIFEST:
            raise ArchiveError("manifest exceeds the size limit")
        try:
            with temporary_manifest.open("xb") as stream:
                stream.write(manifest_bytes)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            raise ArchiveError(f"cannot write temporary manifest: {exc}") from exc
        if _lexists(archive_path) or _lexists(manifest_path):
            raise ArchiveError("archive or manifest destination appeared during creation")
        os.replace(temporary_archive, archive_path)
        archive_installed = True
        os.replace(temporary_manifest, manifest_path)
        return document
    except Exception:
        if archive_installed and not _lexists(manifest_path):
            try:
                archive_path.unlink()
            except OSError:
                pass
        raise
    finally:
        for temporary in (temporary_archive, temporary_manifest):
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _expect_keys(value: object, keys: set[str], label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ArchiveError(f"{label} must be an object")
    actual = set(value)
    if actual != keys:
        missing = sorted(keys - actual)
        unexpected = sorted(actual - keys)
        raise ArchiveError(
            f"{label} keys do not match schema; missing={missing}, unexpected={unexpected}"
        )
    return value


def _validate_sha(value: object, label: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise ArchiveError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _validate_size(value: object, label: str, *, positive: bool = False) -> int:
    if not _is_int(value) or value < (1 if positive else 0) or value >= 2**63:
        qualifier = "positive " if positive else "non-negative "
        raise ArchiveError(f"{label} must be a {qualifier}integer below 2^63")
    return value


def _validate_manifest_document(document: object) -> dict[str, object]:
    root = _expect_keys(
        document,
        {"archive", "exclusions", "format", "members", "version"},
        "manifest",
    )
    if root["format"] != FORMAT_NAME or root["version"] != FORMAT_VERSION:
        raise ArchiveError("manifest format or version is unsupported")
    if root["exclusions"] != list(EXCLUDED_PREFIXES):
        raise ArchiveError("manifest exclusions do not match the exact contract")
    archive = _expect_keys(
        root["archive"],
        {"compression", "sha256", "size", "tar_sha256", "tar_size"},
        "manifest archive",
    )
    if archive["compression"] not in {"none", "zstd"}:
        raise ArchiveError("manifest archive compression is unsupported")
    archive_size = _validate_size(archive["size"], "manifest archive size", positive=True)
    tar_size = _validate_size(archive["tar_size"], "manifest tar size", positive=True)
    if archive_size > MAX_ARCHIVE:
        raise ArchiveError("manifest archive size exceeds the limit")
    if tar_size > MAX_TAR:
        raise ArchiveError("manifest tar size exceeds the limit")
    archive_sha = _validate_sha(archive["sha256"], "manifest archive digest")
    tar_sha = _validate_sha(archive["tar_sha256"], "manifest tar digest")
    if tar_size % tarfile.RECORDSIZE != 0:
        raise ArchiveError("manifest tar size is not deterministic tar record padding")
    if archive["compression"] == "none" and (
        archive_size != tar_size or archive_sha != tar_sha
    ):
        raise ArchiveError("uncompressed archive metadata must equal tar metadata")
    raw_members = root["members"]
    if not isinstance(raw_members, list):
        raise ArchiveError("manifest members must be an array")
    if len(raw_members) > MAX_MEMBER_COUNT:
        raise ArchiveError(f"manifest member count exceeds {MAX_MEMBER_COUNT}")
    paths: list[str] = []
    kinds: dict[str, str] = {}
    folded: dict[str, str] = {}
    total_file_bytes = 0
    validated_members: list[dict[str, object]] = []
    for index, raw_member in enumerate(raw_members):
        member = _expect_keys(
            raw_member,
            {"mode", "path", "sha256", "size", "target", "type"},
            f"manifest member {index}",
        )
        path = _validate_member_path(member["path"])
        if _excluded(path):
            raise ArchiveError(f"manifest contains excluded member: {path}")
        if path in kinds:
            raise ArchiveError(f"duplicate manifest member: {path}")
        portable = path.casefold()
        previous = folded.setdefault(portable, path)
        if previous != path:
            raise ArchiveError(
                f"case-insensitive manifest collision: {previous!r} and {path!r}"
            )
        kind = member["type"]
        if kind not in {"file", "directory", "symlink"}:
            raise ArchiveError(f"manifest member type is unsupported: {path}")
        mode = member["mode"]
        if not _is_int(mode) or mode < 0 or mode > 0o777:
            raise ArchiveError(f"manifest member mode is invalid: {path}")
        size = _validate_size(member["size"], f"manifest member size: {path}")
        digest = _validate_sha(member["sha256"], f"manifest member digest: {path}")
        target = member["target"]
        if kind == "file":
            if not isinstance(target, str) or target:
                raise ArchiveError(f"regular file link target is not normalized: {path}")
            if size > MAX_MEMBER_SIZE:
                raise ArchiveError(f"manifest member exceeds the size limit: {path}")
            total_file_bytes += size
            if total_file_bytes > MAX_TOTAL_FILE_BYTES:
                raise ArchiveError("manifest file bytes exceed the aggregate size limit")
        elif kind == "directory":
            if size != 0 or digest != EMPTY_SHA256 or target != "":
                raise ArchiveError(f"directory metadata is not normalized: {path}")
        else:
            _validate_symlink_target(path, target)
            if mode != 0o777 or size != 0 or digest != EMPTY_SHA256:
                raise ArchiveError(f"symbolic link metadata is not normalized: {path}")
        kinds[path] = kind
        paths.append(path)
        validated_members.append(member)
    if paths != sorted(paths):
        raise ArchiveError("manifest members are not in canonical path order")
    for path in paths:
        parent = path.rpartition("/")[0]
        if parent and kinds.get(parent) != "directory":
            raise ArchiveError(f"manifest member parent is not a directory: {path}")
    _validate_symlink_graph(validated_members)
    if tar_size != _expected_tar_size(validated_members):
        raise ArchiveError("manifest tar size is not the exact canonical USTAR size")
    return root


def _read_manifest(path: Path) -> dict[str, object]:
    _require_plain_path_chain(path, "manifest")
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ArchiveError(f"manifest is not accessible: {path}: {exc}") from exc
    _reject_reparse(path, metadata, "manifest")
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ArchiveError("manifest must be a plain regular file")
    if metadata.st_size > MAX_MANIFEST:
        raise ArchiveError("manifest exceeds the size limit")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        stream = os.fdopen(descriptor, "rb")
    except OSError as exc:
        raise ArchiveError(f"cannot open manifest: {exc}") from exc
    try:
        opened = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
            or opened.st_size != metadata.st_size
        ):
            raise ArchiveError("manifest changed while it was opened")
        raw = stream.read(MAX_MANIFEST + 1)
        if len(raw) != opened.st_size:
            raise ArchiveError("manifest size changed while it was read")
        after = os.fstat(stream.fileno())
        if (
            after.st_dev != opened.st_dev
            or after.st_ino != opened.st_ino
            or after.st_size != opened.st_size
            or after.st_mtime_ns != opened.st_mtime_ns
            or (os.name != "nt" and after.st_ctime_ns != opened.st_ctime_ns)
        ):
            raise ArchiveError("manifest changed while it was read")
    finally:
        stream.close()
    try:
        text = raw.decode("utf-8", "strict")
        document = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except ArchiveError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ArchiveError(f"manifest is not strict UTF-8 JSON: {exc}") from exc
    validated = _validate_manifest_document(document)
    if raw != _canonical_json_bytes(validated):
        raise ArchiveError("manifest JSON is not in canonical encoding")
    return validated


def _open_archive(path: Path) -> tuple[BinaryIO, os.stat_result]:
    _require_plain_path_chain(path, "archive")
    try:
        before = path.lstat()
    except OSError as exc:
        raise ArchiveError(f"archive is not accessible: {path}: {exc}") from exc
    _reject_reparse(path, before, "archive")
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ArchiveError("archive must be a plain regular file")
    if before.st_size > MAX_ARCHIVE:
        raise ArchiveError("archive exceeds the size limit")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        stream = os.fdopen(descriptor, "rb")
    except OSError as exc:
        raise ArchiveError(f"cannot open archive: {exc}") from exc
    opened = os.fstat(stream.fileno())
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_dev != before.st_dev
        or opened.st_ino != before.st_ino
        or opened.st_size != before.st_size
    ):
        stream.close()
        raise ArchiveError("archive changed while it was opened")
    return stream, opened


def _copy_exact(
    source: BinaryIO,
    destination: BinaryIO | None,
    expected_size: int,
) -> tuple[int, str]:
    digest = hashlib.sha256()
    total = 0
    while True:
        chunk = source.read(COPY_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > expected_size:
            raise ArchiveError("payload is larger than its sealed size")
        digest.update(chunk)
        if destination is not None:
            destination.write(chunk)
    if total != expected_size:
        raise ArchiveError("payload size does not match its sealed size")
    return total, digest.hexdigest()


def _materialize_tar(
    archive_path: Path,
    archive_meta: dict[str, object],
    temporary_tar: Path,
    zstd_executable: str,
) -> None:
    expected_archive_size = archive_meta["size"]
    expected_archive_sha = archive_meta["sha256"]
    expected_tar_size = archive_meta["tar_size"]
    expected_tar_sha = archive_meta["tar_sha256"]
    assert isinstance(expected_archive_size, int)
    assert isinstance(expected_archive_sha, str)
    assert isinstance(expected_tar_size, int)
    assert isinstance(expected_tar_sha, str)
    source, opened = _open_archive(archive_path)
    try:
        if opened.st_size != expected_archive_size:
            raise ArchiveError("archive size does not match the manifest")
        _, archive_sha = _copy_exact(source, None, expected_archive_size)
        if archive_sha != expected_archive_sha:
            raise ArchiveError("archive digest does not match the manifest")
        source.seek(0)
        if archive_meta["compression"] == "none":
            with temporary_tar.open("xb") as output:
                _, tar_sha = _copy_exact(source, output, expected_tar_size)
                output.flush()
                os.fsync(output.fileno())
        else:
            _validate_single_zstd_frame(
                source,
                expected_archive_size,
                expected_tar_size,
            )
            source.seek(0)
            with temporary_tar.open("xb") as output, tempfile.TemporaryFile() as errors:
                try:
                    process = subprocess.Popen(
                        [zstd_executable, "-q", "-d", "-c"],
                        stdin=source,
                        stdout=subprocess.PIPE,
                        stderr=errors,
                        shell=False,
                    )
                except OSError as exc:
                    raise ArchiveError(f"cannot start zstd decompressor: {exc}") from exc
                assert process.stdout is not None
                digest = hashlib.sha256()
                total = 0
                with _ProcessDeadline(process) as deadline:
                    try:
                        while True:
                            chunk = process.stdout.read(COPY_CHUNK)
                            if not chunk:
                                break
                            total += len(chunk)
                            if total > expected_tar_size:
                                raise ArchiveError(
                                    "decompressed tar is larger than its sealed size"
                                )
                            digest.update(chunk)
                            output.write(chunk)
                        process.stdout.close()
                        result = process.wait()
                        deadline.raise_if_expired()
                    except BaseException:
                        _kill_and_wait(process)
                        deadline.raise_if_expired()
                        raise
                if result != 0:
                    detail = _stderr_text(errors)
                    suffix = f": {detail}" if detail else ""
                    raise ArchiveError(
                        f"zstd decompressor exited with status {result}{suffix}"
                    )
                if total != expected_tar_size:
                    raise ArchiveError("decompressed tar size does not match the manifest")
                tar_sha = digest.hexdigest()
                output.flush()
                os.fsync(output.fileno())
        if tar_sha != expected_tar_sha:
            raise ArchiveError("decompressed tar digest does not match the manifest")
        after = os.fstat(source.fileno())
        if (
            after.st_dev != opened.st_dev
            or after.st_ino != opened.st_ino
            or after.st_size != opened.st_size
            or after.st_mtime_ns != opened.st_mtime_ns
            or (os.name != "nt" and after.st_ctime_ns != opened.st_ctime_ns)
        ):
            raise ArchiveError("archive changed while it was read")
    finally:
        source.close()


def _check_zero_bytes(path: Path, start: int, end: int, label: str) -> None:
    if end <= start:
        return
    with path.open("rb") as stream:
        stream.seek(start)
        remaining = end - start
        while remaining:
            chunk = stream.read(min(COPY_CHUNK, remaining))
            if len(chunk) == 0:
                raise ArchiveError(f"tar ended inside {label}")
            if chunk.strip(b"\0"):
                raise ArchiveError(f"tar contains non-zero {label}")
            remaining -= len(chunk)


def _manifest_tar_info(expected: dict[str, object]) -> tarfile.TarInfo:
    info = tarfile.TarInfo(str(expected["path"]))
    info.type = {
        "directory": tarfile.DIRTYPE,
        "file": tarfile.REGTYPE,
        "symlink": tarfile.SYMTYPE,
    }[str(expected["type"])]
    info.mode = int(expected["mode"])
    info.uid = info.gid = info.mtime = 0
    info.uname = info.gname = ""
    info.size = int(expected["size"]) if expected["type"] == "file" else 0
    info.linkname = str(expected["target"])
    info.pax_headers = {}
    return info


def _canonical_tar_header(expected: dict[str, object]) -> bytes:
    try:
        return _manifest_tar_info(expected).tobuf(
            tarfile.USTAR_FORMAT,
            "utf-8",
            "strict",
        )
    except (UnicodeError, ValueError, OverflowError) as exc:
        raise ArchiveError(
            f"manifest member is not canonical USTAR: {expected['path']}"
        ) from exc


def _set_deterministic_mtime(path: Path, label: str) -> None:
    try:
        if stat.S_ISLNK(path.lstat().st_mode):
            os.utime(path, ns=(0, 0), follow_symlinks=False)
        else:
            os.utime(path, ns=(0, 0))
    except (NotImplementedError, OSError) as exc:
        raise ArchiveError(f"cannot normalize restored mtime {label}: {exc}") from exc


def _validate_tar_metadata(member: tarfile.TarInfo, expected: dict[str, object]) -> None:
    path = expected["path"]
    expected_type = {
        "directory": tarfile.DIRTYPE,
        "file": tarfile.REGTYPE,
        "symlink": tarfile.SYMTYPE,
    }[str(expected["type"])]
    if member.type != expected_type:
        raise ArchiveError(f"tar member type mismatch: {path}")
    if member.linkname != expected["target"]:
        raise ArchiveError(f"tar link target mismatch: {path}")
    if member.mode != expected["mode"]:
        raise ArchiveError(f"tar member mode mismatch: {path}")
    if member.size != expected["size"]:
        raise ArchiveError(f"tar member size mismatch: {path}")
    if (
        member.uid != 0
        or member.gid != 0
        or member.mtime != 0
        or member.uname != ""
        or member.gname != ""
        or member.devmajor != 0
        or member.devminor != 0
    ):
        raise ArchiveError(f"tar member ownership or timestamp is not normalized: {path}")
    if member.pax_headers:
        raise ArchiveError(f"tar extension metadata is forbidden: {path}")


def _extract_verified_tar(
    tar_path: Path,
    members: list[dict[str, object]],
    temporary_destination: Path,
) -> None:
    expected_header_offset = 0
    padding_ranges: list[tuple[int, int]] = []
    seen: set[str] = set()
    directories: list[tuple[Path, int]] = []
    symlinks: list[tuple[Path, dict[str, object]]] = []
    symlink_final_kinds = _validate_symlink_graph(members)
    index = 0
    try:
        with tar_path.open("rb") as header_stream, tarfile.open(
                tar_path,
                mode="r:",
                format=tarfile.USTAR_FORMAT,
                encoding="utf-8",
                errors="strict",
                errorlevel=2,
            ) as archive:
            if archive.pax_headers:
                raise ArchiveError("global tar extension metadata is forbidden")
            while True:
                member = archive.next()
                if member is None:
                    break
                path = _validate_member_path(member.name)
                if path in seen:
                    raise ArchiveError(f"duplicate tar member: {path}")
                seen.add(path)
                if index >= len(members):
                    raise ArchiveError(f"unexpected tar member: {path}")
                expected = members[index]
                if path != expected["path"]:
                    if any(candidate["path"] == path for candidate in members):
                        raise ArchiveError(f"tar members are out of canonical order: {path}")
                    raise ArchiveError(f"unexpected tar member: {path}")
                if member.offset != expected_header_offset:
                    raise ArchiveError(f"hidden or non-canonical tar record before: {path}")
                if member.offset_data != member.offset + tarfile.BLOCKSIZE:
                    raise ArchiveError(f"tar extension record is forbidden: {path}")
                _validate_tar_metadata(member, expected)
                header_stream.seek(member.offset)
                raw_header = header_stream.read(tarfile.BLOCKSIZE)
                if raw_header != _canonical_tar_header(expected):
                    raise ArchiveError(f"tar member header is not canonical USTAR: {path}")
                target = temporary_destination.joinpath(*path.split("/"))
                _require_plain_directory(target.parent, f"restored parent for {path}")
                if _lexists(target):
                    raise ArchiveError(f"restored member destination already exists: {path}")
                if expected["type"] == "directory":
                    try:
                        target.mkdir()
                    except OSError as exc:
                        raise ArchiveError(
                            f"cannot create restored directory {path}: {exc}"
                        ) from exc
                    _require_plain_directory(target, f"restored directory {path}")
                    digest = EMPTY_SHA256
                    directories.append((target, expected["mode"]))
                elif expected["type"] == "file":
                    try:
                        extracted = archive.extractfile(member)
                    except (OSError, tarfile.TarError) as exc:
                        raise ArchiveError(f"cannot read tar member {path}: {exc}") from exc
                    if extracted is None:
                        raise ArchiveError(f"regular tar member has no payload: {path}")
                    digest_state = hashlib.sha256()
                    total = 0
                    try:
                        with target.open("xb") as output:
                            while True:
                                chunk = extracted.read(COPY_CHUNK)
                                if not chunk:
                                    break
                                total += len(chunk)
                                if total > expected["size"]:
                                    raise ArchiveError(f"tar member exceeds sealed size: {path}")
                                digest_state.update(chunk)
                                output.write(chunk)
                            output.flush()
                            os.fsync(output.fileno())
                    except OSError as exc:
                        raise ArchiveError(f"cannot restore file {path}: {exc}") from exc
                    finally:
                        extracted.close()
                    if total != expected["size"]:
                        raise ArchiveError(f"tar member size mismatch: {path}")
                    digest = digest_state.hexdigest()
                    try:
                        os.chmod(target, expected["mode"])
                    except OSError as exc:
                        raise ArchiveError(f"cannot set restored file mode {path}: {exc}") from exc
                    _set_deterministic_mtime(target, path)
                else:
                    digest = EMPTY_SHA256
                    symlinks.append((target, expected))
                if digest != expected["sha256"]:
                    raise ArchiveError(f"tar member digest mismatch: {path}")
                payload_end = member.offset_data + member.size
                padded_end = _round_up(payload_end, tarfile.BLOCKSIZE)
                padding_ranges.append((payload_end, padded_end))
                expected_header_offset = padded_end
                index += 1
    except ArchiveError:
        raise
    except (OSError, UnicodeError, tarfile.TarError) as exc:
        raise ArchiveError(f"tar payload is invalid: {exc}") from exc
    if index != len(members):
        missing = members[index]["path"]
        raise ArchiveError(f"tar member is missing: {missing}")
    tar_size = tar_path.stat().st_size
    expected_tar_size = _round_up(
        expected_header_offset + (2 * tarfile.BLOCKSIZE), tarfile.RECORDSIZE
    )
    if tar_size != expected_tar_size:
        raise ArchiveError("tar has trailing or missing unrecorded payload")
    for start, end in padding_ranges:
        _check_zero_bytes(tar_path, start, end, "member padding")
    _check_zero_bytes(tar_path, expected_header_offset, tar_size, "end padding")
    for target, expected in symlinks:
        path = str(expected["path"])
        link_target = str(expected["target"])
        _require_plain_directory(target.parent, f"restored parent for {path}")
        if _lexists(target):
            raise ArchiveError(f"restored symbolic link destination already exists: {path}")
        try:
            os.symlink(
                link_target,
                target,
                target_is_directory=symlink_final_kinds[path] == "directory",
            )
            metadata = target.lstat()
            if not stat.S_ISLNK(metadata.st_mode) or os.readlink(target) != link_target:
                raise ArchiveError(f"restored symbolic link changed during creation: {path}")
        except ArchiveError:
            raise
        except OSError as exc:
            raise ArchiveError(f"cannot restore symbolic link {path}: {exc}") from exc
        _set_deterministic_mtime(target, path)
    for directory, mode in reversed(directories):
        try:
            os.chmod(directory, mode)
        except OSError as exc:
            raise ArchiveError(f"cannot set restored directory mode {directory}: {exc}") from exc
        _set_deterministic_mtime(directory, str(directory))
    _set_deterministic_mtime(temporary_destination, ".")


def _required_restore_space(document: dict[str, object]) -> int:
    archive = document["archive"]
    members = document["members"]
    assert isinstance(archive, dict)
    assert isinstance(members, list)
    extracted_allocation = 0
    for member in members:
        assert isinstance(member, dict)
        if member["type"] == "file":
            size = int(member["size"])
            extracted_allocation += _round_up(size, FILESYSTEM_ALLOCATION_UNIT)
    metadata_reserve = len(members) * FILESYSTEM_ALLOCATION_UNIT
    return (
        int(archive["tar_size"])
        + extracted_allocation
        + metadata_reserve
        + RESTORE_FREE_SPACE_RESERVE
    )


def _require_restore_space(parent: Path, document: dict[str, object]) -> None:
    required = _required_restore_space(document)
    try:
        free = shutil.disk_usage(parent).free
    except OSError as exc:
        raise ArchiveError(f"cannot determine restore free space: {exc}") from exc
    if free < required:
        raise ArchiveError(
            "insufficient restore free space: "
            f"need {required} bytes including {RESTORE_FREE_SPACE_RESERVE} bytes reserve, "
            f"have {free} bytes"
        )


def restore_archive(
    archive_path: Path,
    manifest_path: Path,
    destination: Path,
    *,
    zstd_executable: str = "zstd",
) -> dict[str, object]:
    archive_path = _absolute(Path(archive_path))
    manifest_path = _absolute(Path(manifest_path))
    destination = _absolute(Path(destination))
    _require_output_parent(destination, "destination")
    if _lexists(destination):
        raise ArchiveError("restore destination must not already exist")
    if not isinstance(zstd_executable, str) or not zstd_executable:
        raise ArchiveError("zstd executable must be a non-empty argv element")
    document = _read_manifest(manifest_path)
    archive_meta = document["archive"]
    assert isinstance(archive_meta, dict)
    actual_compression = _compression_for_path(archive_path)
    if archive_meta["compression"] != actual_compression:
        raise ArchiveError("archive suffix and manifest compression disagree")
    _require_restore_space(destination.parent, document)
    temporary_tar = _new_temporary_path(
        destination.parent, f".{destination.name}.tar-"
    )
    temporary_destination = Path(
        tempfile.mkdtemp(dir=destination.parent, prefix=f".{destination.name}.restore-")
    )
    renamed = False
    try:
        _materialize_tar(
            archive_path,
            archive_meta,
            temporary_tar,
            zstd_executable,
        )
        members = document["members"]
        assert isinstance(members, list)
        _extract_verified_tar(temporary_tar, members, temporary_destination)
        if _lexists(destination):
            raise ArchiveError("restore destination appeared during verification")
        try:
            os.rename(temporary_destination, destination)
        except OSError as exc:
            raise ArchiveError(f"cannot publish verified restore atomically: {exc}") from exc
        renamed = True
        return document
    finally:
        try:
            temporary_tar.unlink()
        except FileNotFoundError:
            pass
        if not renamed:
            shutil.rmtree(temporary_destination, ignore_errors=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--zstd",
        default="zstd",
        help="zstd executable path (invoked directly without a shell)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create", help="create a sealed archive")
    create.add_argument("--source-dir", type=Path, required=True)
    create.add_argument("--archive", type=Path, required=True)
    create.add_argument("--manifest", type=Path, required=True)
    restore = subparsers.add_parser("restore", help="restore a sealed archive")
    restore.add_argument("--archive", type=Path, required=True)
    restore.add_argument("--manifest", type=Path, required=True)
    restore.add_argument("--destination", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "create":
            document = create_archive(
                args.source_dir,
                args.archive,
                args.manifest,
                zstd_executable=args.zstd,
            )
        else:
            document = restore_archive(
                args.archive,
                args.manifest,
                args.destination,
                zstd_executable=args.zstd,
            )
    except (ArchiveError, OSError) as exc:
        print(f"kernel artifact archive: {exc}", file=sys.stderr)
        return 1
    print(_canonical_json_bytes(document).decode("ascii"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
