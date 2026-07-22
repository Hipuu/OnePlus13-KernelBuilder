#!/usr/bin/env python3
"""Seal and verify the reusable mixed-kernel artifact bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ARCHIVE_NAME = "kernel-build.tar.zst"
ARCHIVE_MANIFEST_NAME = "KERNEL-ARTIFACT-MANIFEST.json"
PROVENANCE_NAME = "KERNEL-ARTIFACT-PROVENANCE.json"
CHECKSUM_NAME = "SHA256SUMS"
BUNDLE_NAMES = frozenset(
    {ARCHIVE_NAME, ARCHIVE_MANIFEST_NAME, PROVENANCE_NAME, CHECKSUM_NAME}
)
SEALED_NAMES = tuple(sorted(BUNDLE_NAMES - {CHECKSUM_NAME}))
FORMAT_NAME = "oneplus13-kernel-artifact-bundle"
FORMAT_VERSION = 1
ARCHIVE_FORMAT = "oneplus13-kernel-build-archive"
ARCHIVE_VERSION = 2
ARCHIVE_EXCLUSIONS = [
    "modules",
    ".op13/config-work",
    ".op13/config-work-msm-kernel",
]
CONTEXT_PATH = ".op13/build-context.json"
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
GIT_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
REPOSITORY_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}\Z")
ARTIFACT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,254}\Z")
BRANDING_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}\Z")
CHECKSUM_LINE_RE = re.compile(
    r"([0-9a-f]{64})  (KERNEL-ARTIFACT-MANIFEST\.json|"
    r"KERNEL-ARTIFACT-PROVENANCE\.json|kernel-build\.tar\.zst)\Z"
)
ALLOWED_WORKFLOWS = {
    ".github/workflows/build.yml": "workflow_dispatch",
    ".github/workflows/release.yml": "workflow_dispatch",
    ".github/workflows/nightly.yml": "schedule",
}
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_BYTES = 16 * 1024 * 1024 * 1024
MAX_TAR_BYTES = 32 * 1024 * 1024 * 1024
MAX_PROVENANCE_BYTES = 64 * 1024
MAX_CONTEXT_BYTES = 64 * 1024 * 1024
MAX_RUN_JSON_BYTES = 4 * 1024 * 1024
HASH_CHUNK = 1024 * 1024
MAX_BUILD_EPOCH = int(
    datetime(2107, 12, 31, 23, 59, 58, tzinfo=timezone.utc).timestamp()
)
MAX_BUILD_TIMESTAMP_BYTES = 1024


class BundleError(RuntimeError):
    """A reusable artifact violates its sealed bundle contract."""


@dataclass(frozen=True)
class BundleIdentity:
    repository: str
    head_sha: str
    run_id: int
    run_attempt: int
    artifact_name: str
    base: str
    root: str
    profile: str
    optimization: str
    lto: str
    branding: str
    build_timestamp: str


@dataclass(frozen=True)
class TimestampRequest:
    mode: str
    artifact_key: str
    requested: str | None
    requested_sha256: str | None
    explicit_epoch: int | None


def _canonical_json_bytes(document: object) -> bytes:
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


def _context_digest(document: dict[str, Any]) -> str:
    unsigned = dict(document)
    unsigned.pop("context_sha256", None)
    payload = (
        json.dumps(
            unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _reject_constant(value: str) -> object:
    raise BundleError(f"non-finite JSON value is forbidden: {value}")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise BundleError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _plain_directory(path: Path, label: str) -> Path:
    raw = Path(path)
    try:
        metadata = raw.lstat()
    except OSError as exc:
        raise BundleError(f"{label} is not accessible: {raw}: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise BundleError(f"{label} must be a plain directory: {raw}")
    try:
        return raw.resolve(strict=True)
    except OSError as exc:
        raise BundleError(f"{label} cannot be resolved: {raw}: {exc}") from exc


def _plain_file(path: Path, label: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise BundleError(f"{label} is not accessible: {path}: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise BundleError(f"{label} must be a plain regular file: {path}")
    return metadata


def _read_plain_bytes(path: Path, label: str, maximum: int) -> bytes:
    before = _plain_file(path, label)
    if before.st_size > maximum:
        raise BundleError(f"{label} exceeds the size limit")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_dev != before.st_dev
                or opened.st_ino != before.st_ino
                or opened.st_size != before.st_size
            ):
                raise BundleError(f"{label} changed while it was opened")
            chunks: list[bytes] = []
            total = 0
            while chunk := stream.read(min(HASH_CHUNK, maximum + 1 - total)):
                chunks.append(chunk)
                total += len(chunk)
                if total > maximum:
                    break
            payload = b"".join(chunks)
            after = os.fstat(stream.fileno())
    except BundleError:
        raise
    except OSError as exc:
        raise BundleError(f"cannot read {label}: {exc}") from exc
    if len(payload) > maximum:
        raise BundleError(f"{label} exceeds the size limit")
    if (
        len(payload) != opened.st_size
        or after.st_dev != opened.st_dev
        or after.st_ino != opened.st_ino
        or after.st_size != opened.st_size
        or after.st_mtime_ns != opened.st_mtime_ns
    ):
        raise BundleError(f"{label} changed while it was read")
    return payload


def _hash_plain_file(
    path: Path,
    label: str,
    maximum: int | None = None,
) -> tuple[int, str]:
    before = _plain_file(path, label)
    if maximum is not None and before.st_size > maximum:
        raise BundleError(f"{label} exceeds the size limit")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    digest = hashlib.sha256()
    total = 0
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_dev != before.st_dev
                or opened.st_ino != before.st_ino
                or opened.st_size != before.st_size
            ):
                raise BundleError(f"{label} changed while it was opened")
            while chunk := stream.read(HASH_CHUNK):
                total += len(chunk)
                digest.update(chunk)
            after = os.fstat(stream.fileno())
    except BundleError:
        raise
    except OSError as exc:
        raise BundleError(f"cannot hash {label}: {exc}") from exc
    if (
        total != opened.st_size
        or after.st_dev != opened.st_dev
        or after.st_ino != opened.st_ino
        or after.st_size != opened.st_size
        or after.st_mtime_ns != opened.st_mtime_ns
    ):
        raise BundleError(f"{label} changed while it was hashed")
    return total, digest.hexdigest()


def _read_json(
    path: Path,
    label: str,
    maximum: int,
    *,
    canonical: bool,
) -> tuple[object, bytes]:
    payload = _read_plain_bytes(path, label, maximum)
    try:
        text = payload.decode("utf-8", "strict")
        document = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except BundleError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BundleError(f"{label} is not strict UTF-8 JSON: {exc}") from exc
    if canonical and payload != _canonical_json_bytes(document):
        raise BundleError(f"{label} is not in canonical JSON encoding")
    return document, payload


def _expect_keys(value: object, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BundleError(f"{label} must be an object")
    actual = set(value)
    if actual != keys:
        raise BundleError(
            f"{label} keys differ; missing={sorted(keys - actual)}, "
            f"unexpected={sorted(actual - keys)}"
        )
    return value


def _require_sha(value: object, label: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise BundleError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _require_positive(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise BundleError(f"{label} must be a positive integer")
    return value


def _validate_build_epoch(value: object, label: str) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
        or value > MAX_BUILD_EPOCH
    ):
        raise BundleError(
            f"{label} must be between 1970-01-01 and 2107-12-31T23:59:58Z"
        )
    return value


def timestamp_request(raw: str) -> TimestampRequest:
    if not isinstance(raw, str):
        raise BundleError("build timestamp must be a string")
    try:
        encoded = raw.encode("utf-8", "strict")
    except UnicodeError as exc:
        raise BundleError("build timestamp must be valid UTF-8") from exc
    if len(encoded) > MAX_BUILD_TIMESTAMP_BYTES:
        raise BundleError("build timestamp exceeds the size limit")
    if "\x00" in raw or "\n" in raw or "\r" in raw:
        raise BundleError("build timestamp must be a single-line value")
    if raw == "":
        return TimestampRequest(
            mode="default",
            artifact_key="default",
            requested=None,
            requested_sha256=None,
            explicit_epoch=None,
        )
    configured = raw.strip()
    if not configured:
        raise BundleError("a non-empty build timestamp must not be only whitespace")
    if configured.isdigit():
        epoch = int(configured)
    else:
        normalized = (
            configured[:-1] + "+00:00"
            if configured.endswith("Z")
            else configured
        )
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise BundleError(
                "build timestamp must be RFC3339 or an epoch integer"
            ) from exc
        if parsed.tzinfo is None:
            raise BundleError("build timestamp must include a timezone")
        epoch = int(parsed.timestamp())
    epoch = _validate_build_epoch(epoch, "explicit build timestamp")
    digest = hashlib.sha256(encoded).hexdigest()
    return TimestampRequest(
        mode="explicit",
        artifact_key=digest,
        requested=raw,
        requested_sha256=digest,
        explicit_epoch=epoch,
    )


def _validate_identity(identity: BundleIdentity) -> None:
    if REPOSITORY_RE.fullmatch(identity.repository) is None:
        raise BundleError("repository must be an owner/name pair")
    if GIT_SHA_RE.fullmatch(identity.head_sha) is None:
        raise BundleError("head SHA must be a lowercase full Git commit")
    _require_positive(identity.run_id, "run ID")
    _require_positive(identity.run_attempt, "run attempt")
    if ARTIFACT_RE.fullmatch(identity.artifact_name) is None:
        raise BundleError("artifact name is invalid")
    if identity.base not in {"oos15-cn", "oos15-global", "oos16"}:
        raise BundleError("base is unsupported")
    if identity.root not in {"kernelsu", "kernelsu-next", "none"}:
        raise BundleError("root variant is unsupported")
    if identity.profile not in {"full", "wild", "nethunter"}:
        raise BundleError("feature profile is unsupported")
    if identity.optimization not in {"O2", "O3"}:
        raise BundleError("optimization is unsupported")
    if identity.lto not in {"thin", "full"}:
        raise BundleError("LTO mode is unsupported")
    if BRANDING_RE.fullmatch(identity.branding) is None:
        raise BundleError("branding is invalid")
    timestamp = timestamp_request(identity.build_timestamp)
    expected_artifact_name = (
        f"kernel-build-{identity.base}-{identity.root}-{identity.profile}-mixed-"
        f"{identity.optimization}-{identity.lto}-{identity.branding}-timestamp-"
        f"{timestamp.artifact_key}"
    )
    if identity.artifact_name != expected_artifact_name:
        raise BundleError("artifact name differs from the exact build identity")


def _bundle_files(root: Path, expected: set[str]) -> dict[str, Path]:
    try:
        children = list(root.iterdir())
    except OSError as exc:
        raise BundleError(f"cannot enumerate bundle directory: {exc}") from exc
    actual = {child.name for child in children}
    if len(actual) != len(children):
        raise BundleError("bundle directory contains duplicate names")
    if actual != expected:
        raise BundleError(
            f"bundle namespace differs; missing={sorted(expected - actual)}, "
            f"unexpected={sorted(actual - expected)}"
        )
    result: dict[str, Path] = {}
    folded: dict[str, str] = {}
    for child in children:
        _plain_file(child, f"bundle member {child.name}")
        previous = folded.setdefault(child.name.casefold(), child.name)
        if previous != child.name:
            raise BundleError(
                f"bundle has a case-insensitive name collision: {previous}, {child.name}"
            )
        result[child.name] = child
    return result


def _archive_context_record(manifest_path: Path) -> tuple[str, int, dict[str, Any]]:
    document, _ = _read_json(
        manifest_path,
        "kernel archive manifest",
        MAX_MANIFEST_BYTES,
        canonical=True,
    )
    manifest = _expect_keys(
        document,
        {"archive", "exclusions", "format", "members", "version"},
        "kernel archive manifest",
    )
    if manifest["format"] != ARCHIVE_FORMAT or manifest["version"] != ARCHIVE_VERSION:
        raise BundleError("kernel archive manifest format or version is unsupported")
    if manifest["exclusions"] != ARCHIVE_EXCLUSIONS:
        raise BundleError("kernel archive exclusions differ from the exact contract")
    archive = _expect_keys(
        manifest["archive"],
        {"compression", "sha256", "size", "tar_sha256", "tar_size"},
        "kernel archive record",
    )
    if archive["compression"] != "zstd":
        raise BundleError("kernel archive must use zstd compression")
    _require_sha(archive["sha256"], "kernel archive digest")
    _require_sha(archive["tar_sha256"], "kernel tar digest")
    archive_size = _require_positive(archive["size"], "kernel archive size")
    tar_size = _require_positive(archive["tar_size"], "kernel tar size")
    if archive_size > MAX_ARCHIVE_BYTES or tar_size > MAX_TAR_BYTES:
        raise BundleError("kernel archive or tar exceeds the size limit")
    members = manifest["members"]
    if not isinstance(members, list):
        raise BundleError("kernel archive members must be an array")
    matches: list[dict[str, Any]] = []
    for member in members:
        if isinstance(member, dict) and member.get("path") == CONTEXT_PATH:
            matches.append(member)
    if len(matches) != 1:
        raise BundleError("kernel archive must contain exactly one build context")
    context = _expect_keys(
        matches[0],
        {"mode", "path", "sha256", "size", "target", "type"},
        "archived build context",
    )
    if context["type"] != "file" or context["target"] != "":
        raise BundleError("archived build context must be a regular file")
    digest = _require_sha(context["sha256"], "archived build-context digest")
    size = _require_positive(context["size"], "archived build-context size")
    if size > MAX_CONTEXT_BYTES:
        raise BundleError("archived build context exceeds the size limit")
    return digest, size, archive


def _validate_context(
    path: Path,
    identity: BundleIdentity,
    expected_sha: str,
    expected_size: int,
) -> int:
    size, digest = _hash_plain_file(
        path, "restored build context", MAX_CONTEXT_BYTES
    )
    if (size, digest) != (expected_size, expected_sha):
        raise BundleError("build context differs from the archive manifest")
    document, _ = _read_json(
        path,
        "restored build context",
        MAX_CONTEXT_BYTES,
        canonical=False,
    )
    if not isinstance(document, dict):
        raise BundleError("build context must be an object")
    if document.get("context_sha256") != _context_digest(document):
        raise BundleError("build context self-digest is invalid")
    if (
        document.get("kind") != "oneplus13-build-context"
        or document.get("schema_version") != 1
        or document.get("stage") != "packaged"
        or document.get("smoke") is not False
        or document.get("device") != "oneplus13"
        or document.get("profile") != identity.base
        or document.get("target") != "sun"
        or document.get("arch") != "arm64"
        or document.get("kmi") != "android15-6.6"
    ):
        raise BundleError("build context platform or stage differs from the bundle identity")
    configuration = document.get("configuration")
    if not isinstance(configuration, dict) or (
        configuration.get("profile") != identity.base
        or configuration.get("feature_profile") != identity.profile
        or configuration.get("root_variant") != identity.root
        or configuration.get("build_target") != "mixed"
        or configuration.get("optimization") != identity.optimization
        or configuration.get("lto") != identity.lto
    ):
        raise BundleError("build configuration differs from the bundle identity")
    kernel = document.get("kernel")
    if not isinstance(kernel, dict) or (
        kernel.get("build_target") != "mixed"
        or kernel.get("branding") != identity.branding
    ):
        raise BundleError("kernel record differs from the mixed prerequisite identity")
    source_date_epoch = _validate_build_epoch(
        kernel.get("source_date_epoch"), "kernel source_date_epoch"
    )
    timestamp = timestamp_request(identity.build_timestamp)
    if (
        timestamp.explicit_epoch is not None
        and source_date_epoch != timestamp.explicit_epoch
    ):
        raise BundleError(
            "kernel source_date_epoch differs from the explicit build timestamp"
        )
    expected_timestamp_record = {
        "artifact_key": timestamp.artifact_key,
        "mode": timestamp.mode,
        "requested": timestamp.requested,
        "requested_sha256": timestamp.requested_sha256,
        "source_date_epoch": source_date_epoch,
    }
    if kernel.get("build_timestamp") != expected_timestamp_record:
        raise BundleError(
            "kernel build_timestamp record differs from the exact timestamp request"
        )
    if not isinstance(document.get("packages"), list) or not document["packages"]:
        raise BundleError("build context has no packaged output evidence")
    return source_date_epoch


def _provenance_document(
    identity: BundleIdentity,
    context_sha: str,
    context_size: int,
    source_date_epoch: int,
) -> dict[str, Any]:
    timestamp = timestamp_request(identity.build_timestamp)
    source_date_epoch = _validate_build_epoch(
        source_date_epoch, "provenance source_date_epoch"
    )
    if (
        timestamp.explicit_epoch is not None
        and source_date_epoch != timestamp.explicit_epoch
    ):
        raise BundleError("provenance epoch differs from the explicit build timestamp")
    return {
        "artifact_name": identity.artifact_name,
        "build": {
            "base": identity.base,
            "branding": identity.branding,
            "build_context": {
                "path": CONTEXT_PATH,
                "sha256": context_sha,
                "size": context_size,
            },
            "build_timestamp": {
                "artifact_key": timestamp.artifact_key,
                "mode": timestamp.mode,
                "requested": timestamp.requested,
                "requested_sha256": timestamp.requested_sha256,
                "source_date_epoch": source_date_epoch,
            },
            "lto": identity.lto,
            "optimization": identity.optimization,
            "prerequisite_target": "mixed",
            "profile": identity.profile,
            "root": identity.root,
        },
        "format": FORMAT_NAME,
        "github": {
            "head_sha": identity.head_sha,
            "repository": identity.repository,
            "run_attempt": identity.run_attempt,
            "run_id": identity.run_id,
        },
        "schema_version": FORMAT_VERSION,
    }


def _checksum_payload(files: dict[str, Path]) -> bytes:
    lines = []
    for name in SEALED_NAMES:
        _, digest = _hash_plain_file(files[name], f"bundle member {name}")
        lines.append(f"{digest}  {name}\n")
    return "".join(lines).encode("ascii")


def _write_new(path: Path, payload: bytes) -> None:
    if os.path.lexists(path):
        raise BundleError(f"bundle output already exists: {path.name}")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.tmp-"
    )
    temporary = Path(temporary_name)
    installed = False
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if os.path.lexists(path):
            raise BundleError(f"bundle output appeared during sealing: {path.name}")
        os.replace(temporary, path)
        installed = True
    finally:
        if not installed:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def seal_bundle(root: Path, context_path: Path, identity: BundleIdentity) -> dict[str, Any]:
    _validate_identity(identity)
    directory = _plain_directory(root, "bundle directory")
    files = _bundle_files(directory, {ARCHIVE_NAME, ARCHIVE_MANIFEST_NAME})
    context_sha, context_size, archive_record = _archive_context_record(
        files[ARCHIVE_MANIFEST_NAME]
    )
    archive_size, archive_sha = _hash_plain_file(
        files[ARCHIVE_NAME], "kernel archive", MAX_ARCHIVE_BYTES
    )
    if (
        archive_size != archive_record["size"]
        or archive_sha != archive_record["sha256"]
    ):
        raise BundleError("kernel archive differs from its external manifest")
    source_date_epoch = _validate_context(
        Path(context_path), identity, context_sha, context_size
    )
    provenance = _provenance_document(
        identity, context_sha, context_size, source_date_epoch
    )
    provenance_path = directory / PROVENANCE_NAME
    checksum_path = directory / CHECKSUM_NAME
    try:
        _write_new(provenance_path, _canonical_json_bytes(provenance))
        files = _bundle_files(directory, set(BUNDLE_NAMES - {CHECKSUM_NAME}))
        _write_new(checksum_path, _checksum_payload(files))
        return verify_bundle(directory, identity, restored_dir=None)
    except Exception:
        for generated in (checksum_path, provenance_path):
            try:
                generated.unlink()
            except FileNotFoundError:
                pass
        raise


def _verify_checksums(files: dict[str, Path]) -> None:
    payload = _read_plain_bytes(
        files[CHECKSUM_NAME], CHECKSUM_NAME, 4096
    )
    if not payload or b"\r" in payload or not payload.endswith(b"\n"):
        raise BundleError("SHA256SUMS must be non-empty LF-terminated text")
    try:
        text = payload.decode("ascii", "strict")
    except UnicodeError as exc:
        raise BundleError("SHA256SUMS must be ASCII") from exc
    listed: dict[str, str] = {}
    for line in text.splitlines():
        match = CHECKSUM_LINE_RE.fullmatch(line)
        if match is None:
            raise BundleError(f"invalid SHA256SUMS line: {line!r}")
        digest, name = match.groups()
        if name in listed:
            raise BundleError(f"duplicate SHA256SUMS entry: {name}")
        listed[name] = digest
    if tuple(listed) != SEALED_NAMES:
        raise BundleError("SHA256SUMS does not have exact canonical coverage and order")
    maximums = {
        ARCHIVE_NAME: MAX_ARCHIVE_BYTES,
        ARCHIVE_MANIFEST_NAME: MAX_MANIFEST_BYTES,
        PROVENANCE_NAME: MAX_PROVENANCE_BYTES,
    }
    for name, expected in listed.items():
        _, observed = _hash_plain_file(
            files[name], f"bundle member {name}", maximums[name]
        )
        if observed != expected:
            raise BundleError(f"bundle checksum mismatch: {name}")


def verify_bundle(
    root: Path,
    identity: BundleIdentity,
    *,
    restored_dir: Path | None,
    allow_earlier_run_attempt: bool = False,
) -> dict[str, Any]:
    _validate_identity(identity)
    directory = _plain_directory(root, "bundle directory")
    files = _bundle_files(directory, set(BUNDLE_NAMES))
    _verify_checksums(files)
    context_sha, context_size, archive_record = _archive_context_record(
        files[ARCHIVE_MANIFEST_NAME]
    )
    archive_size, archive_sha = _hash_plain_file(
        files[ARCHIVE_NAME], "kernel archive", MAX_ARCHIVE_BYTES
    )
    if (
        archive_size != archive_record["size"]
        or archive_sha != archive_record["sha256"]
    ):
        raise BundleError("kernel archive differs from its external manifest")
    provenance, _ = _read_json(
        files[PROVENANCE_NAME],
        "kernel artifact provenance",
        MAX_PROVENANCE_BYTES,
        canonical=True,
    )
    if not isinstance(provenance, dict):
        raise BundleError("kernel artifact provenance must be an object")
    provenance_build = provenance.get("build")
    if not isinstance(provenance_build, dict):
        raise BundleError("kernel artifact provenance build record is absent")
    provenance_timestamp = provenance_build.get("build_timestamp")
    if not isinstance(provenance_timestamp, dict):
        raise BundleError("kernel artifact provenance timestamp record is absent")
    source_date_epoch = _validate_build_epoch(
        provenance_timestamp.get("source_date_epoch"),
        "provenance source_date_epoch",
    )
    provenance_github = provenance.get("github")
    if not isinstance(provenance_github, dict):
        raise BundleError("kernel artifact provenance GitHub record is absent")
    provenance_run_attempt = _require_positive(
        provenance_github.get("run_attempt"), "provenance run attempt"
    )
    if allow_earlier_run_attempt:
        if provenance_run_attempt > identity.run_attempt:
            raise BundleError(
                "kernel artifact provenance run attempt is newer than the source run"
            )
        expected_identity = BundleIdentity(
            repository=identity.repository,
            head_sha=identity.head_sha,
            run_id=identity.run_id,
            run_attempt=provenance_run_attempt,
            artifact_name=identity.artifact_name,
            base=identity.base,
            root=identity.root,
            profile=identity.profile,
            optimization=identity.optimization,
            lto=identity.lto,
            branding=identity.branding,
            build_timestamp=identity.build_timestamp,
        )
    else:
        expected_identity = identity
    expected = _provenance_document(
        expected_identity, context_sha, context_size, source_date_epoch
    )
    if provenance != expected:
        raise BundleError("kernel artifact provenance differs from the expected identity")
    restored = False
    if restored_dir is not None:
        restored_root = _plain_directory(restored_dir, "restored kernel directory")
        metadata_root = _plain_directory(
            restored_root / ".op13", "restored kernel metadata directory"
        )
        if metadata_root != restored_root / ".op13":
            raise BundleError("restored kernel metadata directory resolves unexpectedly")
        restored_epoch = _validate_context(
            metadata_root / "build-context.json",
            identity,
            context_sha,
            context_size,
        )
        if restored_epoch != source_date_epoch:
            raise BundleError(
                "restored kernel source_date_epoch differs from artifact provenance"
            )
        restored = True
    return {
        "artifact_name": identity.artifact_name,
        "build_context_sha256": context_sha,
        "format": FORMAT_NAME,
        "head_sha": identity.head_sha,
        "repository": identity.repository,
        "restored_context_verified": restored,
        "source_date_epoch": source_date_epoch,
        "timestamp_key": timestamp_request(identity.build_timestamp).artifact_key,
        "run_attempt": provenance_run_attempt,
        "source_run_attempt": identity.run_attempt,
        "run_id": identity.run_id,
        "status": "verified",
    }


def verify_workflow_run(
    document: object,
    *,
    repository: str,
    repository_id: int,
    head_sha: str,
    run_id: int,
    requested_run_id: int | None,
    current_run_id: int,
) -> dict[str, Any]:
    if REPOSITORY_RE.fullmatch(repository) is None:
        raise BundleError("repository must be an owner/name pair")
    _require_positive(repository_id, "repository ID")
    if GIT_SHA_RE.fullmatch(head_sha) is None:
        raise BundleError("head SHA must be a lowercase full Git commit")
    _require_positive(run_id, "run ID")
    _require_positive(current_run_id, "current run ID")
    if requested_run_id is not None:
        _require_positive(requested_run_id, "requested run ID")
    if not isinstance(document, dict):
        raise BundleError("workflow run response must be an object")
    if document.get("id") != run_id or document.get("head_sha") != head_sha:
        raise BundleError("workflow run ID or head SHA differs")
    run_attempt = _require_positive(document.get("run_attempt"), "source run attempt")
    source_repository = document.get("repository")
    head_repository = document.get("head_repository")
    if not isinstance(source_repository, dict) or not isinstance(head_repository, dict):
        raise BundleError("workflow run repository identity is absent")
    if (
        source_repository.get("id") != repository_id
        or source_repository.get("full_name") != repository
        or head_repository.get("id") != repository_id
        or head_repository.get("full_name") != repository
    ):
        raise BundleError("workflow run came from a different repository or fork")
    path = document.get("path")
    event = document.get("event")
    if path not in ALLOWED_WORKFLOWS or event != ALLOWED_WORKFLOWS[path]:
        raise BundleError("workflow path or event is not trusted for kernel artifacts")
    completed = document.get("status") == "completed" and document.get("conclusion") == "success"
    current_release = (
        path == ".github/workflows/release.yml"
        and event == "workflow_dispatch"
        and document.get("status") == "in_progress"
        and document.get("conclusion") is None
        and requested_run_id is not None
        and requested_run_id == current_run_id == run_id
    )
    if not completed and not current_release:
        raise BundleError("workflow run is not a successful completed trusted run")
    if requested_run_id is not None and run_id != requested_run_id:
        raise BundleError("workflow run differs from the explicitly requested run")
    return {
        "event": event,
        "head_sha": head_sha,
        "path": path,
        "repository": repository,
        "run_attempt": run_attempt,
        "run_id": run_id,
        "status": "trusted",
    }


def _positive_cli(value: str) -> int:
    if not value.isascii() or not value.isdigit() or value.startswith("0"):
        raise argparse.ArgumentTypeError("must be a canonical positive integer")
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _identity_from_args(args: argparse.Namespace) -> BundleIdentity:
    return BundleIdentity(
        repository=args.repository,
        head_sha=args.head_sha,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
        artifact_name=args.artifact_name,
        base=args.base,
        root=args.root,
        profile=args.profile,
        optimization=args.optimization,
        lto=args.lto,
        branding=args.branding,
        build_timestamp=args.build_timestamp,
    )


def _add_identity_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repository", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--run-id", required=True, type=_positive_cli)
    parser.add_argument("--run-attempt", required=True, type=_positive_cli)
    parser.add_argument("--artifact-name", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--optimization", required=True)
    parser.add_argument("--lto", required=True)
    parser.add_argument("--branding", required=True)
    parser.add_argument("--build-timestamp", required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    seal = commands.add_parser("seal", help="write provenance and canonical checksums")
    seal.add_argument("--directory", required=True, type=Path)
    seal.add_argument("--build-context", required=True, type=Path)
    _add_identity_arguments(seal)
    verify = commands.add_parser("verify", help="verify the exact artifact bundle")
    verify.add_argument("--directory", required=True, type=Path)
    verify.add_argument("--restored-dir", type=Path)
    verify.add_argument(
        "--allow-earlier-run-attempt",
        action="store_true",
        help=(
            "accept a sealed earlier attempt of the same trusted workflow run; "
            "--run-attempt remains the current source-run upper bound"
        ),
    )
    _add_identity_arguments(verify)
    run = commands.add_parser("verify-run", help="verify GitHub workflow-run metadata")
    run.add_argument("--run-json", required=True, type=Path)
    run.add_argument("--repository", required=True)
    run.add_argument("--repository-id", required=True, type=_positive_cli)
    run.add_argument("--head-sha", required=True)
    run.add_argument("--run-id", required=True, type=_positive_cli)
    run.add_argument("--requested-run-id", type=_positive_cli)
    run.add_argument("--current-run-id", required=True, type=_positive_cli)
    timestamp = commands.add_parser(
        "timestamp-key", help="derive the safe artifact key for an exact timestamp input"
    )
    timestamp.add_argument("--build-timestamp", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "seal":
            result = seal_bundle(
                args.directory,
                args.build_context,
                _identity_from_args(args),
            )
        elif args.command == "verify":
            result = verify_bundle(
                args.directory,
                _identity_from_args(args),
                restored_dir=args.restored_dir,
                allow_earlier_run_attempt=args.allow_earlier_run_attempt,
            )
        elif args.command == "verify-run":
            document, _ = _read_json(
                args.run_json,
                "workflow run response",
                MAX_RUN_JSON_BYTES,
                canonical=False,
            )
            result = verify_workflow_run(
                document,
                repository=args.repository,
                repository_id=args.repository_id,
                head_sha=args.head_sha,
                run_id=args.run_id,
                requested_run_id=args.requested_run_id,
                current_run_id=args.current_run_id,
            )
        else:
            timestamp = timestamp_request(args.build_timestamp)
            result = {
                "artifact_key": timestamp.artifact_key,
                "mode": timestamp.mode,
                "requested_sha256": timestamp.requested_sha256,
            }
    except (BundleError, OSError, ValueError) as exc:
        print(f"kernel artifact bundle: {exc}", file=sys.stderr)
        return 1
    print(_canonical_json_bytes(result).decode("ascii"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
