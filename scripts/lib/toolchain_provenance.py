"""Fail-closed provenance for the compiler selected by the OnePlus build."""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
import stat
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from .config import is_full_commit, sha256_file
from .context import atomic_write_json
from .errors import BuildToolError


KIND = "oneplus13-build-toolchain-provenance"
SCHEMA_VERSION = 2
CLANG_VERSION_RE = re.compile(
    r"^CLANG_VERSION=([A-Za-z0-9][A-Za-z0-9._-]{0,63})$"
)
COMMON_PROJECT_PATH = "kernel_platform/common"
CLANG_PROJECT_PATH = "kernel_platform/prebuilts/clang/host/linux-x86"
CLANG_VERSION_DECLARATION = f"{COMMON_PROJECT_PATH}/build.config.constants"
ROOT_PROJECT_PATH = "."
BUILD_TOOLS_PROJECT_PATH = "kernel_platform/prebuilts/build-tools"
KERNEL_BUILD_TOOLS_PROJECT_PATH = (
    "kernel_platform/prebuilts/kernel-build-tools"
)
BAZEL_ENTRYPOINT = "kernel_platform/tools/bazel"
BAZEL_ENTRYPOINT_TARGET = "../build/kernel/kleaf/bazel.sh"
BAZEL_WRAPPER = "kernel_platform/build/kernel/kleaf/bazel.sh"
BAZEL_ORIGIN = "kernel_platform/build/kernel/kleaf/bazel.origin.sh"
BAZEL_DRIVER = "kernel_platform/build/kernel/kleaf/bazel.py"
BAZEL_GETTOP = "kernel_platform/build/kernel/gettop.sh"
BAZEL_PYTHON = (
    f"{BUILD_TOOLS_PROJECT_PATH}/path/linux-x86/python3"
)
BAZEL_BINARY_RELATIVE = (
    "prebuilts/kernel-build-tools/bazel/linux-x86_64/bazel"
)
BAZEL_BINARY = f"kernel_platform/{BAZEL_BINARY_RELATIVE}"
BAZEL_BINARY_RE = re.compile(
    r'^_BAZEL_REL_PATH = "([^"]+)"$',
    re.MULTILINE,
)

# With LLVM=1 and LLVM_IAS=1, the official OnePlus setup maps these variables
# to the corresponding unprefixed binaries in the selected Clang bin directory.
OFFICIAL_TOOL_VARIABLES: Mapping[str, tuple[str, ...]] = {
    "clang": ("AS", "CC", "HOSTCC"),
    "clang++": ("HOSTCXX",),
    "ld.lld": ("LD",),
    "llvm-ar": ("AR",),
    "llvm-nm": ("NM",),
    "llvm-objcopy": ("OBJCOPY",),
    "llvm-objdump": ("OBJDUMP",),
    "llvm-readelf": ("READELF",),
    "llvm-size": ("OBJSIZE",),
    "llvm-strip": ("STRIP",),
}
# These commands come from the hosted runner rather than a resolved-manifest
# project.  Record them as mutable environment evidence; never present their
# paths or versions as pinned inputs.  The list covers the build/storage
# preflight plus the core tools used directly by this repository's pipeline.
HOST_ENVIRONMENT_TOOLS = (
    "awk",
    "bash",
    "curl",
    "date",
    "depmod",
    "df",
    "find",
    "gh",
    "git",
    "jq",
    "make",
    "patch",
    "ps",
    "realpath",
    "sed",
    "setsid",
    "sha256sum",
    "tar",
    "unzip",
    "xz",
    "zip",
    "zstd",
)
LICENSE_METADATA_PATTERNS = (
    "LICENSE",
    "LICENSE.*",
    "LICENSE-*",
    "MODULE_LICENSE*",
    "NOTICE",
    "NOTICE.*",
    "NOTICE-*",
)
BUILD_METADATA_PATTERNS = (
    "Android.bp",
    "Android.mk",
    "BUILD",
    "BUILD.*",
    "METADATA",
    "MODULE.bazel",
    "WORKSPACE",
    "WORKSPACE.bazel",
    "clang_source_info.md",
)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _portable_manifest_path(value: str | None, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise BuildToolError(f"{where}: expected a non-empty checkout path")
    if value == "./":
        return "."
    if "\\" in value:
        raise BuildToolError(f"{where}: checkout path uses a backslash")
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or ".." in parsed.parts:
        raise BuildToolError(f"{where}: checkout path escapes the source tree")
    normalized = parsed.as_posix()
    if normalized != value or normalized in {"", "/"}:
        raise BuildToolError(f"{where}: checkout path is not canonical: {value!r}")
    return normalized


def _parse_manifest(path: Path) -> list[dict[str, str]]:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise BuildToolError(f"cannot read resolved manifest {path}: {exc}") from exc
    upper = payload.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise BuildToolError("resolved manifest contains a prohibited XML declaration")
    try:
        document = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise BuildToolError(f"invalid resolved manifest {path}: {exc}") from exc
    if document.tag != "manifest":
        raise BuildToolError("resolved manifest root element must be <manifest>")

    result: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    for index, project in enumerate(document.findall("project")):
        name = project.get("name")
        if not isinstance(name, str) or not name:
            raise BuildToolError(
                f"resolved manifest project[{index}] has no project name"
            )
        checkout_path = _portable_manifest_path(
            project.get("path") or name,
            f"resolved manifest project[{index}]",
        )
        if checkout_path in seen_paths:
            raise BuildToolError(
                f"resolved manifest repeats checkout path {checkout_path}"
            )
        seen_paths.add(checkout_path)
        revision = project.get("revision")
        if not is_full_commit(revision):
            raise BuildToolError(
                f"resolved manifest project {checkout_path} is not pinned "
                "to a lowercase 40-character commit"
            )
        result.append(
            {
                "name": name,
                "path": checkout_path,
                "commit": str(revision),
            }
        )
    if not result:
        raise BuildToolError("resolved manifest contains no projects")
    return sorted(result, key=lambda item: item["path"])


def _manifest_project_for(
    relative: str,
    projects: Sequence[Mapping[str, str]],
) -> Mapping[str, str] | None:
    candidates = [
        project
        for project in projects
        if project["path"] == "."
        or relative == project["path"]
        or relative.startswith(project["path"] + "/")
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda item: len(item["path"].split("/")))


def _canonical_source_file(
    source_root: Path,
    relative: str,
    label: str,
) -> tuple[Path, Path]:
    selected = source_root.joinpath(*PurePosixPath(relative).parts)
    if not selected.is_file():
        raise BuildToolError(f"{label} is missing: {relative}")
    try:
        canonical = selected.resolve(strict=True)
    except OSError as exc:
        raise BuildToolError(f"cannot resolve {label} {relative}: {exc}") from exc
    if not _inside(canonical, source_root):
        raise BuildToolError(
            f"{label} resolves outside the synced source tree: {relative} -> {canonical}"
        )
    return selected, canonical


def _source_relative(path: Path, source_root: Path) -> str:
    try:
        return path.relative_to(source_root).as_posix()
    except ValueError as exc:
        raise BuildToolError(
            f"path is outside the synced source tree: {path}"
        ) from exc


def _bind_project(
    path: Path,
    source_root: Path,
    projects: Sequence[Mapping[str, str]],
    *,
    expected_project_path: str,
    label: str,
) -> dict[str, str]:
    relative = _source_relative(path, source_root)
    project = _manifest_project_for(relative, projects)
    if project is None:
        raise BuildToolError(
            f"{label} is not bound to any resolved-manifest project: {relative}"
        )
    if project["path"] != expected_project_path:
        raise BuildToolError(
            f"{label} is bound to resolved-manifest project {project['path']}, "
            f"expected {expected_project_path}"
        )
    return dict(project)


def _run_git(
    project_root: Path,
    arguments: Sequence[str],
    *,
    label: str,
    input_bytes: bytes | None = None,
) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), *arguments],
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BuildToolError(f"cannot inspect Git tree for {label}: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise BuildToolError(
            f"cannot inspect Git tree for {label}: "
            f"git {' '.join(arguments)} exited {result.returncode}: "
            f"{detail or '<no error output>'}"
        )
    return result.stdout


def _validated_project_checkout(
    *,
    source_root: Path,
    project: Mapping[str, str],
    label: str,
    project_cache: dict[tuple[str, str], dict[str, str]],
) -> tuple[Path, dict[str, str]]:
    project_path = project["path"]
    manifest_commit = project["commit"]
    cache_key = (project_path, manifest_commit)
    cached = project_cache.get(cache_key)
    project_root = _project_root(source_root, project_path)
    if cached is not None:
        return project_root, cached

    top_level_raw = _run_git(
        project_root,
        ["rev-parse", "--show-toplevel"],
        label=label,
    )
    try:
        top_level = Path(top_level_raw.decode("utf-8").strip()).resolve(strict=True)
    except (OSError, UnicodeError) as exc:
        raise BuildToolError(
            f"resolved-manifest project {project_path} has invalid Git metadata"
        ) from exc
    if top_level != project_root:
        raise BuildToolError(
            f"resolved-manifest project {project_path} is not an independent "
            f"Git checkout: {top_level}"
        )
    _run_git(
        project_root,
        ["cat-file", "-e", f"{manifest_commit}^{{commit}}"],
        label=label,
    )
    head = _run_git(
        project_root,
        ["rev-parse", "HEAD"],
        label=label,
    ).decode("ascii", errors="strict").strip()
    if head != manifest_commit:
        raise BuildToolError(
            f"{label} checkout HEAD {head} differs from resolved-manifest "
            f"commit {manifest_commit}"
        )
    record = {
        "project_path": project_path,
        "manifest_commit": manifest_commit,
        "checkout_head": head,
    }
    project_cache[cache_key] = record
    return project_root, record


def _git_tree_file_binding(
    path: Path,
    *,
    source_root: Path,
    project: Mapping[str, str],
    label: str,
    project_cache: dict[tuple[str, str], dict[str, str]],
    file_cache: dict[tuple[str, str, str], dict[str, Any]],
) -> dict[str, Any]:
    """Prove one worktree file equals its declared manifest-commit blob.

    The comparison is deliberately independent of the index: regular-file
    bytes, executable mode, and symbolic-link target are compared directly to
    the Git tree entry at the resolved-manifest commit.
    """

    project_root, checkout = _validated_project_checkout(
        source_root=source_root,
        project=project,
        label=label,
        project_cache=project_cache,
    )
    try:
        # Resolve only the parent so Windows 8.3 aliases are normalized without
        # dereferencing the selected symlink whose literal tree entry we need.
        normalized_path = path.parent.resolve(strict=True) / path.name
        relative = normalized_path.relative_to(project_root).as_posix()
    except ValueError as exc:
        raise BuildToolError(
            f"{label} is outside resolved-manifest project {project['path']}: {path}"
        ) from exc
    if not relative or relative == ".":
        raise BuildToolError(f"{label} does not name a file inside its Git project")
    cache_key = (project["path"], project["commit"], relative)
    cached = file_cache.get(cache_key)
    if cached is not None:
        return dict(cached)

    tree_output = _run_git(
        project_root,
        ["ls-tree", "-z", project["commit"], "--", relative],
        label=label,
    )
    entries = [entry for entry in tree_output.split(b"\0") if entry]
    if len(entries) != 1 or b"\t" not in entries[0]:
        raise BuildToolError(
            f"{label} is absent or ambiguous in resolved-manifest commit "
            f"{project['commit']}: {relative}"
        )
    metadata, tree_path = entries[0].split(b"\t", 1)
    try:
        tree_mode, tree_type, tree_oid = metadata.decode("ascii").split()
    except (UnicodeError, ValueError) as exc:
        raise BuildToolError(f"{label} has an invalid Git tree entry") from exc
    if tree_path != os.fsencode(relative):
        raise BuildToolError(f"{label} Git tree path differs from its checkout path")
    if tree_type != "blob" or tree_mode not in {"100644", "100755", "120000"}:
        raise BuildToolError(
            f"{label} has unsupported Git tree type/mode {tree_type}/{tree_mode}"
        )

    try:
        status = path.lstat()
    except OSError as exc:
        raise BuildToolError(f"cannot inspect {label} worktree file: {exc}") from exc
    if stat.S_ISLNK(status.st_mode):
        actual_mode = "120000"
        try:
            payload = os.fsencode(os.readlink(path))
        except OSError as exc:
            raise BuildToolError(f"cannot read {label} symbolic link: {exc}") from exc
    elif stat.S_ISREG(status.st_mode):
        actual_mode = "100755" if status.st_mode & 0o111 else "100644"
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise BuildToolError(f"cannot read {label} worktree file: {exc}") from exc
    else:
        raise BuildToolError(f"{label} is not a regular file or symbolic link")

    # Windows checkouts cannot reliably represent POSIX executable bits.  Git
    # still verifies content and symlink kind there; Linux (the build runner)
    # additionally compares the live executable mode.
    if actual_mode == "120000" or os.name != "nt":
        if actual_mode != tree_mode:
            raise BuildToolError(
                f"{label} worktree mode {actual_mode} differs from manifest "
                f"tree mode {tree_mode}"
            )
    elif tree_mode == "120000":
        raise BuildToolError(
            f"{label} manifest entry is a symbolic link but the checkout is regular"
        )

    worktree_oid = _run_git(
        project_root,
        ["hash-object", "--stdin"],
        label=label,
        input_bytes=payload,
    ).decode("ascii", errors="strict").strip()
    if worktree_oid != tree_oid:
        raise BuildToolError(
            f"{label} worktree content differs from resolved-manifest commit "
            f"{project['commit']}: {relative}"
        )
    record: dict[str, Any] = {
        **checkout,
        "path": relative,
        "tree_mode": tree_mode,
        "tree_type": tree_type,
        "tree_object": tree_oid,
        "worktree_object": worktree_oid,
        "status": "exact-manifest-tree",
    }
    file_cache[cache_key] = record
    return dict(record)


def _file_kind(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            prefix = handle.read(4)
    except OSError as exc:
        raise BuildToolError(f"cannot inspect tool kind for {path}: {exc}") from exc
    if prefix == b"\x7fELF":
        return "elf"
    if prefix.startswith(b"#!"):
        return "script"
    return "other"


def _normalize_version_output(value: str, source_root: Path) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    spellings = sorted(
        {str(source_root), source_root.as_posix()},
        key=len,
        reverse=True,
    )
    for spelling in spellings:
        if spelling:
            normalized = normalized.replace(spelling, "${SOURCE_ROOT}")
    return "\n".join(line.rstrip() for line in normalized.splitlines()).strip()


def _run_version(
    command: Sequence[str],
    *,
    source_root: Path,
    path_prefix: Path | None = None,
    required: bool,
) -> tuple[str, str | None]:
    environment = dict(os.environ)
    environment.update({"LANG": "C", "LC_ALL": "C", "TZ": "UTC"})
    if path_prefix is not None:
        inherited = environment.get("PATH", "")
        environment["PATH"] = str(path_prefix) + (
            os.pathsep + inherited if inherited else ""
        )
    try:
        result = subprocess.run(
            list(command),
            cwd=source_root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        if required:
            raise BuildToolError(
                f"version probe failed for {Path(command[0]).name}: {exc}"
            ) from exc
        return "", str(exc)
    output = _normalize_version_output(result.stdout or "", source_root)
    if result.returncode != 0 or not output:
        message = (
            f"version probe for {Path(command[0]).name} exited "
            f"{result.returncode}: {output or '<no output>'}"
        )
        if required:
            raise BuildToolError(message)
        return output, message
    return output, None


def _metadata_matches(name: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def _nearest_metadata(
    tool: Path,
    project_root: Path,
    source_root: Path,
    patterns: Sequence[str],
) -> dict[str, Any]:
    directory = tool.parent
    distance = 0
    while True:
        try:
            entries = sorted(directory.iterdir(), key=lambda path: path.name)
        except OSError as exc:
            raise BuildToolError(
                f"cannot inspect toolchain metadata in {directory}: {exc}"
            ) from exc
        matches: list[dict[str, Any]] = []
        for candidate in entries:
            if not _metadata_matches(candidate.name, patterns) or not candidate.is_file():
                continue
            try:
                canonical = candidate.resolve(strict=True)
            except OSError as exc:
                raise BuildToolError(
                    f"cannot resolve toolchain metadata {candidate}: {exc}"
                ) from exc
            if not _inside(canonical, project_root):
                raise BuildToolError(
                    f"toolchain metadata resolves outside its pinned project: {candidate}"
                )
            matches.append(
                {
                    "path": _source_relative(candidate, source_root),
                    "canonical_path": _source_relative(canonical, source_root),
                    "size": canonical.stat().st_size,
                    "sha256": sha256_file(canonical),
                }
            )
        if matches:
            return {"ancestor_distance": distance, "files": matches}
        if directory == project_root:
            return {"ancestor_distance": None, "files": []}
        if not _inside(directory, project_root):
            raise BuildToolError(
                f"compiler tool is outside its manifest project: {tool}"
            )
        directory = directory.parent
        distance += 1


def _project_root(
    source_root: Path,
    project_path: str,
) -> Path:
    if project_path == ROOT_PROJECT_PATH:
        return source_root
    candidate = source_root.joinpath(*PurePosixPath(project_path).parts)
    try:
        canonical = candidate.resolve(strict=True)
    except OSError as exc:
        raise BuildToolError(
            f"resolved-manifest project checkout is missing: {project_path}"
        ) from exc
    if not canonical.is_dir() or not _inside(canonical, source_root):
        raise BuildToolError(
            f"resolved-manifest project checkout escapes the source tree: "
            f"{project_path}"
        )
    return canonical


def _metadata_for_file(
    *,
    canonical: Path,
    project_path: str,
    source_root: Path,
    metadata_cache: dict[tuple[str, str, str], dict[str, Any]],
) -> dict[str, Any]:
    cache_base = _source_relative(canonical.parent, source_root)
    project_root = _project_root(source_root, project_path)
    license_key = (project_path, cache_base, "license")
    build_key = (project_path, cache_base, "build")
    if license_key not in metadata_cache:
        metadata_cache[license_key] = _nearest_metadata(
            canonical,
            project_root,
            source_root,
            LICENSE_METADATA_PATTERNS,
        )
    if build_key not in metadata_cache:
        metadata_cache[build_key] = _nearest_metadata(
            canonical,
            project_root,
            source_root,
            BUILD_METADATA_PATTERNS,
        )
    return {
        "license_or_notice": metadata_cache[license_key],
        "build": metadata_cache[build_key],
    }


def _pinned_component_record(
    *,
    role: str,
    selected: Path,
    canonical: Path,
    source_root: Path,
    projects: Sequence[Mapping[str, str]],
    expected_project_path: str,
    metadata_cache: dict[tuple[str, str, str], dict[str, Any]],
    git_project_cache: dict[tuple[str, str], dict[str, str]],
    git_file_cache: dict[tuple[str, str, str], dict[str, Any]],
    probe_version: bool,
) -> dict[str, Any]:
    selected_project = _bind_project(
        selected,
        source_root,
        projects,
        expected_project_path=expected_project_path,
        label=f"{role} selected path",
    )
    project = _bind_project(
        canonical,
        source_root,
        projects,
        expected_project_path=expected_project_path,
        label=f"{role} canonical path",
    )
    if selected_project != project:
        raise BuildToolError(
            f"{role} selected and canonical paths bind to different manifest projects"
        )
    selected_git_tree = _git_tree_file_binding(
        selected,
        source_root=source_root,
        project=selected_project,
        label=f"{role} selected path",
        project_cache=git_project_cache,
        file_cache=git_file_cache,
    )
    canonical_git_tree = _git_tree_file_binding(
        canonical,
        source_root=source_root,
        project=project,
        label=f"{role} canonical path",
        project_cache=git_project_cache,
        file_cache=git_file_cache,
    )
    record: dict[str, Any] = {
        "role": role,
        "selected_path": _source_relative(selected, source_root),
        "canonical_path": _source_relative(canonical, source_root),
        "selected_path_is_symlink": selected.is_symlink(),
        "manifest_project": project,
        "selected_git_tree": selected_git_tree,
        "canonical_git_tree": canonical_git_tree,
        "size": canonical.stat().st_size,
        "sha256": sha256_file(canonical),
        "kind": _file_kind(canonical),
        "nearest_metadata": _metadata_for_file(
            canonical=canonical,
            project_path=project["path"],
            source_root=source_root,
            metadata_cache=metadata_cache,
        ),
    }
    if probe_version:
        version_output, _ = _run_version(
            [str(selected), "--version"],
            source_root=source_root,
            required=True,
        )
        record.update(
            {
                "version_probe": "--version",
                "version_output": version_output,
            }
        )
    else:
        record.update(
            {
                "version_probe": "not-applicable-source-launcher",
                "version_output": None,
            }
        )
    return record


def _read_utf8_source(path: Path, label: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise BuildToolError(f"cannot read {label} {path}: {exc}") from exc


def _require_marker(value: str, marker: str, label: str) -> None:
    count = value.count(marker)
    if count != 1:
        raise BuildToolError(
            f"{label} must contain exactly one {marker!r} marker; found {count}"
        )


def _inspect_bazel_launcher(
    *,
    source_root: Path,
    projects: Sequence[Mapping[str, str]],
    git_project_cache: dict[tuple[str, str], dict[str, str]],
    git_file_cache: dict[tuple[str, str, str], dict[str, Any]],
) -> dict[str, Any]:
    entry_selected, entry_canonical = _canonical_source_file(
        source_root,
        BAZEL_ENTRYPOINT,
        "Bazel entrypoint",
    )
    if not entry_selected.is_symlink():
        raise BuildToolError(
            f"Bazel entrypoint is not the expected symlink: {BAZEL_ENTRYPOINT}"
        )
    entry_target = os.readlink(entry_selected)
    if entry_target != BAZEL_ENTRYPOINT_TARGET:
        raise BuildToolError(
            f"Bazel entrypoint target changed: expected "
            f"{BAZEL_ENTRYPOINT_TARGET!r}, got {entry_target!r}"
        )
    _, wrapper_canonical = _canonical_source_file(
        source_root,
        BAZEL_WRAPPER,
        "OnePlus Bazel wrapper",
    )
    if entry_canonical != wrapper_canonical:
        raise BuildToolError(
            "Bazel entrypoint does not resolve to the OnePlus Bazel wrapper"
        )
    wrapper_source = _read_utf8_source(
        wrapper_canonical,
        "OnePlus Bazel wrapper",
    )
    _require_marker(
        wrapper_source,
        "original_sh=$my_dir/bazel.origin.sh",
        "OnePlus Bazel wrapper",
    )

    origin_selected, origin_canonical = _canonical_source_file(
        source_root,
        BAZEL_ORIGIN,
        "upstream Bazel launcher",
    )
    origin_source = _read_utf8_source(
        origin_canonical,
        "upstream Bazel launcher",
    )
    for marker in (
        "prebuilts/build-tools/path/linux-x86/python3",
        "gettop.sh",
        "bazel.py",
    ):
        _require_marker(origin_source, marker, "upstream Bazel launcher")

    gettop_selected, gettop_canonical = _canonical_source_file(
        source_root,
        BAZEL_GETTOP,
        "Bazel repository-discovery helper",
    )
    python_selected, python_canonical = _canonical_source_file(
        source_root,
        BAZEL_PYTHON,
        "pinned Bazel Python interpreter",
    )
    driver_selected, driver_canonical = _canonical_source_file(
        source_root,
        BAZEL_DRIVER,
        "Bazel Python driver",
    )
    driver_source = _read_utf8_source(
        driver_canonical,
        "Bazel Python driver",
    )
    binary_matches = BAZEL_BINARY_RE.findall(driver_source)
    if binary_matches != [BAZEL_BINARY_RELATIVE]:
        raise BuildToolError(
            "Bazel Python driver must select exactly "
            f"{BAZEL_BINARY_RELATIVE!r}; found {binary_matches}"
        )
    binary_selected, binary_canonical = _canonical_source_file(
        source_root,
        BAZEL_BINARY,
        "pinned Bazel binary",
    )

    metadata_cache: dict[tuple[str, str, str], dict[str, Any]] = {}
    entry_record = _pinned_component_record(
        role="entrypoint-and-oneplus-wrapper",
        selected=entry_selected,
        canonical=entry_canonical,
        source_root=source_root,
        projects=projects,
        expected_project_path=ROOT_PROJECT_PATH,
        metadata_cache=metadata_cache,
        git_project_cache=git_project_cache,
        git_file_cache=git_file_cache,
        probe_version=False,
    )
    entry_record["symlink_target"] = entry_target
    components = [
        entry_record,
        _pinned_component_record(
            role="upstream-launcher",
            selected=origin_selected,
            canonical=origin_canonical,
            source_root=source_root,
            projects=projects,
            expected_project_path=ROOT_PROJECT_PATH,
            metadata_cache=metadata_cache,
            git_project_cache=git_project_cache,
            git_file_cache=git_file_cache,
            probe_version=False,
        ),
        _pinned_component_record(
            role="repository-discovery-helper",
            selected=gettop_selected,
            canonical=gettop_canonical,
            source_root=source_root,
            projects=projects,
            expected_project_path=ROOT_PROJECT_PATH,
            metadata_cache=metadata_cache,
            git_project_cache=git_project_cache,
            git_file_cache=git_file_cache,
            probe_version=False,
        ),
        _pinned_component_record(
            role="launcher-python-interpreter",
            selected=python_selected,
            canonical=python_canonical,
            source_root=source_root,
            projects=projects,
            expected_project_path=BUILD_TOOLS_PROJECT_PATH,
            metadata_cache=metadata_cache,
            git_project_cache=git_project_cache,
            git_file_cache=git_file_cache,
            probe_version=True,
        ),
        _pinned_component_record(
            role="bazel-python-driver",
            selected=driver_selected,
            canonical=driver_canonical,
            source_root=source_root,
            projects=projects,
            expected_project_path=ROOT_PROJECT_PATH,
            metadata_cache=metadata_cache,
            git_project_cache=git_project_cache,
            git_file_cache=git_file_cache,
            probe_version=False,
        ),
        _pinned_component_record(
            role="bazel-binary",
            selected=binary_selected,
            canonical=binary_canonical,
            source_root=source_root,
            projects=projects,
            expected_project_path=KERNEL_BUILD_TOOLS_PROJECT_PATH,
            metadata_cache=metadata_cache,
            git_project_cache=git_project_cache,
            git_file_cache=git_file_cache,
            probe_version=True,
        ),
    ]
    return {
        "entrypoint": BAZEL_ENTRYPOINT,
        "entrypoint_target": BAZEL_ENTRYPOINT_TARGET,
        "driver_binary_relative": BAZEL_BINARY_RELATIVE,
        "components": components,
    }


def _compiler_tool_record(
    *,
    name: str,
    selected: Path,
    canonical: Path,
    source_root: Path,
    clang_bin: Path,
    projects: Sequence[Mapping[str, str]],
    metadata_cache: dict[tuple[str, str, str], dict[str, Any]],
    git_project_cache: dict[tuple[str, str], dict[str, str]],
    git_file_cache: dict[tuple[str, str, str], dict[str, Any]],
) -> dict[str, Any]:
    selected_project = _bind_project(
        selected,
        source_root,
        projects,
        expected_project_path=CLANG_PROJECT_PATH,
        label=f"compiler tool {name} selected path",
    )
    project = _bind_project(
        canonical,
        source_root,
        projects,
        expected_project_path=CLANG_PROJECT_PATH,
        label=f"compiler tool {name} canonical path",
    )
    if selected_project != project:
        raise BuildToolError(
            f"compiler tool {name} selected and canonical paths bind to "
            "different manifest projects"
        )
    selected_git_tree = _git_tree_file_binding(
        selected,
        source_root=source_root,
        project=selected_project,
        label=f"compiler tool {name} selected path",
        project_cache=git_project_cache,
        file_cache=git_file_cache,
    )
    canonical_git_tree = _git_tree_file_binding(
        canonical,
        source_root=source_root,
        project=project,
        label=f"compiler tool {name} canonical path",
        project_cache=git_project_cache,
        file_cache=git_file_cache,
    )
    version_output, _ = _run_version(
        [str(selected), "--version"],
        source_root=source_root,
        path_prefix=clang_bin,
        required=True,
    )
    stat_result = canonical.stat()
    return {
        "name": name,
        "official_variables": list(OFFICIAL_TOOL_VARIABLES[name]),
        "selected_path": _source_relative(selected, source_root),
        "canonical_path": _source_relative(canonical, source_root),
        "selected_path_is_symlink": selected.is_symlink(),
        "manifest_project": project,
        "selected_git_tree": selected_git_tree,
        "canonical_git_tree": canonical_git_tree,
        "size": stat_result.st_size,
        "sha256": sha256_file(canonical),
        "kind": _file_kind(canonical),
        "version_output": version_output,
        "nearest_metadata": _metadata_for_file(
            canonical=canonical,
            project_path=project["path"],
            source_root=source_root,
            metadata_cache=metadata_cache,
        ),
    }


def _environment_tool_records(source_root: Path) -> list[dict[str, Any]]:
    selected: list[tuple[str, str | None]] = [
        (name, shutil.which(name)) for name in HOST_ENVIRONMENT_TOOLS
    ]
    selected.append(("python3", sys.executable or None))
    records: list[dict[str, Any]] = []
    for name, value in sorted(selected):
        base: dict[str, Any] = {
            "name": name,
            "provenance": "environment-provided",
            "immutable": False,
            "version_probe": ["--version"],
        }
        if not value:
            base["status"] = "missing"
            records.append(base)
            continue
        try:
            canonical = Path(value).resolve(strict=True)
        except OSError as exc:
            base.update({"status": "unavailable", "error": str(exc)})
            records.append(base)
            continue
        version, error = _run_version(
            [str(canonical), "--version"],
            source_root=source_root,
            required=False,
        )
        base.update(
            {
                "canonical_path": str(canonical),
                "status": "available" if error is None else "version-unavailable",
                "version_output": version,
            }
        )
        if error is not None:
            base["version_error"] = error
        records.append(base)
    return records


def _github_runner_image_record() -> dict[str, Any]:
    values: dict[str, str | None] = {}
    for environment_name, record_name in (
        ("ImageOS", "image_os"),
        ("ImageVersion", "image_version"),
    ):
        raw = os.environ.get(environment_name)
        value = raw.strip() if isinstance(raw, str) else ""
        if len(value) > 256 or "\x00" in value or "\n" in value or "\r" in value:
            raise BuildToolError(
                f"GitHub runner environment {environment_name} is invalid"
            )
        values[record_name] = value or None
    status = "recorded" if all(values.values()) else (
        "partial" if any(values.values()) else "not-provided"
    )
    return {
        "provenance": "github-actions-environment",
        "immutable": False,
        "status": status,
        **values,
    }


def inspect_build_toolchain(
    source_dir: Path,
    resolved_manifest: Path,
) -> dict[str, Any]:
    """Inspect and bind the official compiler selection without changing it."""

    try:
        source_root = source_dir.resolve(strict=True)
    except OSError as exc:
        raise BuildToolError(f"synced source tree is missing: {source_dir}") from exc
    if not source_root.is_dir():
        raise BuildToolError(f"synced source tree is not a directory: {source_root}")

    try:
        manifest_canonical = resolved_manifest.resolve(strict=True)
    except OSError as exc:
        raise BuildToolError(
            f"resolved manifest is missing: {resolved_manifest}"
        ) from exc
    if not manifest_canonical.is_file() or not _inside(manifest_canonical, source_root):
        raise BuildToolError(
            "resolved manifest must be a regular file inside the synced source tree"
        )
    projects = _parse_manifest(manifest_canonical)

    git_project_cache: dict[tuple[str, str], dict[str, str]] = {}
    git_file_cache: dict[tuple[str, str, str], dict[str, Any]] = {}

    declaration_selected, declaration_canonical = _canonical_source_file(
        source_root,
        CLANG_VERSION_DECLARATION,
        "Clang version declaration",
    )
    declaration_project = _bind_project(
        declaration_canonical,
        source_root,
        projects,
        expected_project_path=COMMON_PROJECT_PATH,
        label="Clang version declaration",
    )
    try:
        declaration_text = declaration_canonical.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise BuildToolError(
            f"cannot read Clang version declaration {declaration_canonical}: {exc}"
        ) from exc
    matches = [
        match.group(1)
        for line in declaration_text.splitlines()
        if (match := CLANG_VERSION_RE.fullmatch(line)) is not None
    ]
    if len(matches) != 1:
        raise BuildToolError(
            "locked common tree must declare exactly one CLANG_VERSION"
        )
    declaration_selected_project = _bind_project(
        declaration_selected,
        source_root,
        projects,
        expected_project_path=COMMON_PROJECT_PATH,
        label="Clang version declaration selected path",
    )
    if declaration_selected_project != declaration_project:
        raise BuildToolError(
            "Clang version declaration selected and canonical paths bind to "
            "different manifest projects"
        )
    declaration_selected_git_tree = _git_tree_file_binding(
        declaration_selected,
        source_root=source_root,
        project=declaration_selected_project,
        label="Clang version declaration selected path",
        project_cache=git_project_cache,
        file_cache=git_file_cache,
    )
    declaration_canonical_git_tree = _git_tree_file_binding(
        declaration_canonical,
        source_root=source_root,
        project=declaration_project,
        label="Clang version declaration canonical path",
        project_cache=git_project_cache,
        file_cache=git_file_cache,
    )
    clang_version = matches[0]
    clang_bin_relative = (
        f"{CLANG_PROJECT_PATH}/clang-{clang_version}/bin"
    )
    clang_bin = source_root.joinpath(*PurePosixPath(clang_bin_relative).parts)

    compiler_tools: list[dict[str, Any]] = []
    metadata_cache: dict[tuple[str, str, str], dict[str, Any]] = {}
    missing: list[str] = []
    selected_tools: dict[str, tuple[Path, Path]] = {}
    for name in sorted(OFFICIAL_TOOL_VARIABLES):
        relative = f"{clang_bin_relative}/{name}"
        selected = source_root.joinpath(*PurePosixPath(relative).parts)
        if not selected.is_file():
            missing.append(name)
            continue
        selected_tools[name] = _canonical_source_file(
            source_root,
            relative,
            f"compiler tool {name}",
        )
    if missing:
        raise BuildToolError(
            "locked Clang toolchain is incomplete: " + ", ".join(missing)
        )
    for name in sorted(selected_tools):
        selected, canonical = selected_tools[name]
        compiler_tools.append(
            _compiler_tool_record(
                name=name,
                selected=selected,
                canonical=canonical,
                source_root=source_root,
                clang_bin=clang_bin,
                projects=projects,
                metadata_cache=metadata_cache,
                git_project_cache=git_project_cache,
                git_file_cache=git_file_cache,
            )
        )
    bazel_launcher = _inspect_bazel_launcher(
        source_root=source_root,
        projects=projects,
        git_project_cache=git_project_cache,
        git_file_cache=git_file_cache,
    )

    return {
        "kind": KIND,
        "schema_version": SCHEMA_VERSION,
        "path_scope": "synced-source-relative",
        "resolved_manifest": {
            "path": _source_relative(manifest_canonical, source_root),
            "size": manifest_canonical.stat().st_size,
            "sha256": sha256_file(manifest_canonical),
        },
        "selection": {
            "environment": {"LLVM": "1", "LLVM_IAS": "1"},
            "declaration": {
                "path": _source_relative(declaration_selected, source_root),
                "canonical_path": _source_relative(
                    declaration_canonical, source_root
                ),
                "size": declaration_canonical.stat().st_size,
                "sha256": sha256_file(declaration_canonical),
                "manifest_project": declaration_project,
                "selected_git_tree": declaration_selected_git_tree,
                "canonical_git_tree": declaration_canonical_git_tree,
            },
            "clang_version": clang_version,
            "toolchain_bin": clang_bin_relative,
            "manifest_project": next(
                dict(project)
                for project in projects
                if project["path"] == CLANG_PROJECT_PATH
            ),
        },
        "compiler_tools": compiler_tools,
        "bazel_launcher": bazel_launcher,
        "host_environment_tools": _environment_tool_records(source_root),
        "github_runner_image": _github_runner_image_record(),
    }


def record_build_toolchain(
    source_dir: Path,
    resolved_manifest: Path,
    outputs: Sequence[Path],
) -> dict[str, Any]:
    if not outputs:
        raise BuildToolError("at least one provenance output path is required")
    document = inspect_build_toolchain(source_dir, resolved_manifest)
    written: set[Path] = set()
    for output in outputs:
        canonical_output = output.resolve()
        if canonical_output in written:
            continue
        written.add(canonical_output)
        atomic_write_json(output, document)
    return document
