"""Ordered, data-driven patch and integration operations."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from .config import (
    DependencyLock,
    FeatureProfile,
    Profile,
    load_json_yaml,
    resolve_inside,
    sha256_file,
)
from .context import (
    advance_context,
    feature_selection,
    load_context,
    validate_lineage,
    write_context,
)
from .errors import BuildToolError
from .runtime import CommandRunner, fetch_dependencies


OPERATION_TYPES = {"apply", "git-apply", "copy", "replace", "append", "exec"}
ROOT_VARIANTS = {"kernelsu", "kernelsu-next", "none"}
KERNEL_TREE_ORDER = ("common", "msm-kernel")
KERNEL_TREE_PLACEHOLDER = "{kernel_tree}"
KERNEL_TREE_SUBSTITUTION_FIELDS = frozenset(
    {"cwd", "target", "destination", "argv", "expected_outputs"}
)
KERNEL_TREE_SCALAR_FIELDS = frozenset({"cwd", "target", "destination"})
KERNEL_TREE_LIST_FIELDS = frozenset({"argv", "expected_outputs"})
EXEC_STATIC_PLACEHOLDERS = frozenset(
    {
        "source_dir",
        "cache_root",
        "dependency_dir",
        "repo_root",
        "base",
        "root_variant",
    }
)
FAILURE_REASON_LIMIT = 8_192
FAILURE_TRACEBACK_LIMIT = 16_384


def _patch_utility() -> str:
    executable = shutil.which("patch")
    if executable:
        return executable
    git = shutil.which("git")
    if git:
        bundled = Path(git).resolve().parents[1] / "usr" / "bin" / "patch.exe"
        if bundled.is_file():
            return str(bundled)
    raise BuildToolError("GNU patch is required for an operation with explicit fuzz")


def _safe_relative(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise BuildToolError(f"{where}: expected a non-empty relative path")
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise BuildToolError(f"{where}: path must remain relative")
    return value


def _as_string_list(value: Any, where: str) -> list[str]:
    if not isinstance(value, list):
        raise BuildToolError(f"{where}: expected an array")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise BuildToolError(f"{where}: entries must be non-empty strings")
        result.append(item)
    return result


def _validate_exec_argv_placeholders(
    argv: Iterable[str],
    declared_dependencies: set[str],
    where: str,
) -> None:
    for token in argv:
        placeholders = re.findall(r"\{([^{}]+)\}", token)
        for placeholder in placeholders:
            if placeholder in EXEC_STATIC_PLACEHOLDERS:
                continue
            prefix = "dependency_dir:"
            if placeholder.startswith(prefix):
                dependency_id = placeholder[len(prefix) :]
                if dependency_id in declared_dependencies:
                    continue
                raise BuildToolError(
                    f"{where}: dependency placeholder {dependency_id!r} is not declared"
                )
            raise BuildToolError(
                f"{where}: unsupported argv placeholder {{{placeholder}}}"
            )
        remainder = re.sub(r"\{[^{}]+\}", "", token)
        if "{" in remainder or "}" in remainder:
            raise BuildToolError(f"{where}: malformed argv placeholder {token!r}")
        if "http://" in token or "https://" in token:
            raise BuildToolError(f"{where}: network arguments are forbidden")


def _contains_kernel_tree_placeholder(value: Any) -> bool:
    if isinstance(value, str):
        return KERNEL_TREE_PLACEHOLDER in value
    if isinstance(value, list):
        return any(_contains_kernel_tree_placeholder(item) for item in value)
    if isinstance(value, dict):
        return any(_contains_kernel_tree_placeholder(item) for item in value.values())
    return False


def _validate_kernel_tree_fanout(
    operation: Mapping[str, Any],
    where: str,
) -> tuple[str, ...] | None:
    if "kernel_tree" in operation:
        raise BuildToolError(f"{where}: kernel_tree is reserved for expanded operations")

    has_placeholder = _contains_kernel_tree_placeholder(operation)
    if "kernel_trees" not in operation:
        if has_placeholder:
            raise BuildToolError(
                f"{where}: unresolved {KERNEL_TREE_PLACEHOLDER} placeholder without kernel_trees"
            )
        return None

    raw_trees = _as_string_list(operation["kernel_trees"], f"{where}.kernel_trees")
    if not raw_trees:
        raise BuildToolError(f"{where}.kernel_trees: expected a non-empty array")
    if len(set(raw_trees)) != len(raw_trees):
        raise BuildToolError(f"{where}.kernel_trees: entries must be unique")
    unknown = sorted(set(raw_trees) - set(KERNEL_TREE_ORDER))
    if unknown:
        raise BuildToolError(f"{where}.kernel_trees: unknown kernel trees {unknown}")
    if not has_placeholder:
        raise BuildToolError(
            f"{where}: kernel_trees requires at least one {KERNEL_TREE_PLACEHOLDER} placeholder"
        )

    for field, value in operation.items():
        if field == "kernel_trees" or field in KERNEL_TREE_SUBSTITUTION_FIELDS:
            continue
        if _contains_kernel_tree_placeholder(value):
            supported = ", ".join(sorted(KERNEL_TREE_SUBSTITUTION_FIELDS))
            raise BuildToolError(
                f"{where}.{field}: {KERNEL_TREE_PLACEHOLDER} is supported only in {supported}"
            )
    for field in KERNEL_TREE_SCALAR_FIELDS:
        if field in operation:
            value = operation[field]
            if not isinstance(value, str) or not value:
                raise BuildToolError(
                    f"{where}.{field}: kernel-tree fan-out requires a non-empty string"
                )
    for field in KERNEL_TREE_LIST_FIELDS:
        if field in operation:
            _as_string_list(operation[field], f"{where}.{field}")

    selected = set(raw_trees)
    return tuple(tree for tree in KERNEL_TREE_ORDER if tree in selected)


def _expand_kernel_tree_operation(
    operation: Mapping[str, Any],
    where: str | None = None,
) -> list[dict[str, Any]]:
    location = where or f"operation {operation.get('id', '<unknown>')}"
    trees = _validate_kernel_tree_fanout(operation, location)
    if trees is None:
        return [dict(operation)]

    operation_id = operation.get("id")
    if not isinstance(operation_id, str) or not operation_id:
        raise BuildToolError(f"{location}: operation needs an id before kernel-tree expansion")
    expanded_operations: list[dict[str, Any]] = []
    for tree in trees:
        expanded = dict(operation)
        expanded.pop("kernel_trees", None)
        expanded["id"] = f"{operation_id}@{tree}"
        expanded["kernel_tree"] = tree
        for field in KERNEL_TREE_SUBSTITUTION_FIELDS:
            if field not in expanded:
                continue
            value = expanded[field]
            if isinstance(value, str):
                expanded[field] = value.replace(KERNEL_TREE_PLACEHOLDER, tree)
            elif isinstance(value, list):
                expanded[field] = [
                    item.replace(KERNEL_TREE_PLACEHOLDER, tree) for item in value
                ]
        if _contains_kernel_tree_placeholder(expanded):
            raise BuildToolError(
                f"{location}: unresolved {KERNEL_TREE_PLACEHOLDER} placeholder after expansion"
            )
        expanded_operations.append(expanded)
    return expanded_operations


def _load_series(path: Path) -> tuple[str, list[dict[str, Any]]]:
    raw = load_json_yaml(path)
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise BuildToolError(f"{path}: invalid patch series schema")
    series_id = raw.get("id")
    if not isinstance(series_id, str) or not series_id:
        raise BuildToolError(f"{path}: patch series needs an id")
    if path.stem != series_id:
        raise BuildToolError(f"{path}: patch series id must match filename")
    operations_raw = raw.get("operations")
    if not isinstance(operations_raw, list):
        raise BuildToolError(f"{path}: operations must be an array")
    operations: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw_operation in enumerate(operations_raw):
        where = f"{path}:operations[{index}]"
        if not isinstance(raw_operation, dict):
            raise BuildToolError(f"{where}: operation must be an object")
        operation = dict(raw_operation)
        operation_id = operation.get("id")
        operation_type = operation.get("type")
        if not isinstance(operation_id, str) or not operation_id:
            raise BuildToolError(f"{where}: operation needs an id")
        if operation_id in seen:
            raise BuildToolError(f"{path}: duplicate operation id {operation_id}")
        seen.add(operation_id)
        if operation_type not in OPERATION_TYPES:
            raise BuildToolError(f"{where}: unsupported operation type {operation_type!r}")
        optional = operation.get("optional", False)
        if not isinstance(optional, bool):
            raise BuildToolError(f"{where}: optional must be boolean")
        operation["optional"] = optional
        if "bases" in operation:
            bases = _as_string_list(operation["bases"], f"{where}.bases")
            unknown = sorted(set(bases) - {"oos15-cn", "oos15-global", "oos16"})
            if unknown:
                raise BuildToolError(f"{where}: unknown bases {unknown}")
        if "root_variants" in operation:
            roots = _as_string_list(operation["root_variants"], f"{where}.root_variants")
            unknown_roots = sorted(set(roots) - ROOT_VARIANTS)
            if unknown_roots:
                raise BuildToolError(f"{where}: unknown root variants {unknown_roots}")
        if "dependencies" in operation:
            dependencies = _as_string_list(operation["dependencies"], f"{where}.dependencies")
            if len(set(dependencies)) != len(dependencies):
                raise BuildToolError(f"{where}: dependencies must be unique")
        if "fuzz" in operation:
            fuzz = operation["fuzz"]
            if operation_type != "apply":
                raise BuildToolError(f"{where}: fuzz is supported only for apply operations")
            if not isinstance(fuzz, int) or isinstance(fuzz, bool) or fuzz < 0 or fuzz > 3:
                raise BuildToolError(f"{where}: fuzz must be an integer from 0 through 3")
        if "directory" in operation:
            if operation_type not in {"apply", "git-apply"}:
                raise BuildToolError(
                    f"{where}: directory is supported only for patch operations"
                )
            operation["directory"] = _safe_relative(
                operation["directory"],
                f"{where}.directory",
            )
            if operation.get("fuzz", 0):
                raise BuildToolError(
                    f"{where}: directory is incompatible with fuzzy patch operations"
                )
        if "sha256" in operation:
            expected_sha256 = operation["sha256"]
            if operation_type not in {"apply", "git-apply"}:
                raise BuildToolError(f"{where}: sha256 is supported only for patch operations")
            if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
                raise BuildToolError(f"{where}: sha256 must be a lowercase SHA-256 digest")
        kernel_trees = _validate_kernel_tree_fanout(operation, where)
        if kernel_trees is not None:
            operation["kernel_trees"] = list(kernel_trees)
        operations.append(operation)
    return series_id, operations


def _series_paths(root: Path, feature: FeatureProfile, root_variant: str) -> list[Path]:
    paths: list[Path] = []
    root_series = root / "patches" / "series" / "root.yml"
    if root_variant != "none":
        if not root_series.is_file():
            raise BuildToolError("root integration requested but patches/series/root.yml is missing")
        paths.append(root_series)
    for index, raw in enumerate(feature.patch_series):
        if isinstance(raw, dict):
            name = raw.get("series") or raw.get("id") or raw.get("path")
        else:
            name = raw
        if not isinstance(name, str) or not name:
            raise BuildToolError(f"{feature.source_path}: invalid patch series entry {index}")
        candidate = Path(name)
        if candidate.suffix in {".patch", ".diff"}:
            direct = resolve_inside(root, name, f"patch series entry {name}")
            raise BuildToolError(
                f"{feature.source_path}: direct patches are unsupported here; add {direct} to a named series"
            )
        if candidate.suffix not in {".yml", ".yaml"}:
            candidate = Path("patches") / "series" / f"{name}.yml"
        path = resolve_inside(root, candidate.as_posix(), f"patch series {name}")
        if path in paths:
            continue
        paths.append(path)
    return paths


def _operation_enabled(
    operation: Mapping[str, Any],
    feature: FeatureProfile,
    base: str,
    root_variant: str,
) -> bool:
    if "bases" in operation and base not in operation["bases"]:
        return False
    if "root_variants" in operation and root_variant not in operation["root_variants"]:
        return False
    flag = operation.get("feature") or operation.get("feature_flag")
    if flag is not None:
        if not isinstance(flag, str) or flag not in feature.flags:
            raise BuildToolError(f"operation {operation.get('id')}: unknown feature flag {flag!r}")
        if not feature.flags[flag]:
            return False
    if root_variant == "none" and isinstance(flag, str) and (
        flag.startswith("root.") or "susfs" in flag.lower() or "kernelsu" in flag.lower()
    ):
        return False
    return True


def _dependency_dir(cache_root: Path, lock: DependencyLock, dependency_id: str) -> Path:
    try:
        dependency = lock.dependencies[dependency_id]
    except KeyError as exc:
        raise BuildToolError(f"patch operation references unlocked dependency {dependency_id!r}") from exc
    if dependency.kind == "git":
        return (cache_root / "git" / dependency.id).resolve()
    suffixes = Path(dependency.url).suffixes
    suffix = "".join(suffixes[-2:]) if len(suffixes) >= 2 and suffixes[-2] == ".tar" else (suffixes[-1] if suffixes else "")
    return (cache_root / "files" / f"{dependency.id}-{dependency.sha256[:12]}{suffix}").resolve()


def _verify_dependency_checkout(
    cache_root: Path,
    lock: DependencyLock,
    dependency_id: str,
    runner: CommandRunner,
) -> Path:
    path = _dependency_dir(cache_root, lock, dependency_id)
    dependency = lock.dependencies[dependency_id]
    if dependency.kind == "git":
        if not (path / ".git").exists():
            raise BuildToolError(f"operation dependency {dependency_id} is not a verified Git checkout")
        head = runner.run(["git", "rev-parse", "HEAD"], cwd=path, capture=True).stdout.strip()
        if head != dependency.commit:
            raise BuildToolError(f"operation dependency {dependency_id} checkout does not match its lock")
        origin = runner.run(["git", "remote", "get-url", "origin"], cwd=path, capture=True).stdout.strip()
        if origin.rstrip("/") != dependency.url.rstrip("/"):
            raise BuildToolError(f"operation dependency {dependency_id} origin does not match its lock")
    else:
        if not path.is_file() or sha256_file(path) != dependency.sha256:
            raise BuildToolError(f"operation dependency {dependency_id} file does not match its lock")
    return path


def _operation_source(
    operation: Mapping[str, Any],
    *,
    root: Path,
    cache_root: Path,
    lock: DependencyLock,
    base: str,
) -> tuple[Path, Path]:
    raw_path: Any
    if "path_by_base" in operation:
        mapping = operation["path_by_base"]
        if not isinstance(mapping, dict):
            raise BuildToolError(f"operation {operation['id']}: path_by_base must be an object")
        raw_path = mapping.get(base)
        if raw_path is None:
            raise BuildToolError(f"operation {operation['id']}: no path for base {base}")
    else:
        raw_path = operation.get("path")
    relative = _safe_relative(raw_path, f"operation {operation['id']}.path")
    dependency_id = operation.get("dependency")
    if dependency_id is None:
        base_dir = root.resolve()
    else:
        if not isinstance(dependency_id, str) or not dependency_id:
            raise BuildToolError(f"operation {operation['id']}: invalid dependency")
        base_dir = _dependency_dir(cache_root, lock, dependency_id)
    source = (base_dir / relative).resolve()
    try:
        source.relative_to(base_dir)
    except ValueError as exc:
        raise BuildToolError(f"operation {operation['id']}: source escapes its dependency") from exc
    return source, base_dir


def _git_blob_bytes(checkout: Path, relative: Path, operation_id: str) -> bytes:
    revision = f"HEAD:{relative.as_posix()}"
    result = subprocess.run(
        ["git", "-C", str(checkout), "show", revision],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise BuildToolError(f"operation {operation_id}: failed to read pinned patch blob: {detail}")
    return result.stdout


def _source_target(source_dir: Path, operation: Mapping[str, Any], field: str) -> Path:
    relative = _safe_relative(operation.get(field), f"operation {operation['id']}.{field}")
    result = (source_dir / relative).resolve()
    try:
        result.relative_to(source_dir.resolve())
    except ValueError as exc:
        raise BuildToolError(f"operation {operation['id']}: target escapes source tree") from exc
    return result


def _cwd(source_dir: Path, operation: Mapping[str, Any]) -> Path:
    value = operation.get("cwd", ".")
    return _source_target(source_dir, {**operation, "cwd": value}, "cwd")


def _copy_tree_strict(source: Path, destination: Path) -> None:
    if destination.exists():
        raise BuildToolError(f"copy destination already exists: {destination}")
    for path in source.rglob("*"):
        if ".git" in path.relative_to(source).parts:
            continue
        if path.is_symlink():
            raise BuildToolError(f"symlinks are rejected in dependency overlays: {path}")
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns(".git"))


def _fuzzy_patch_targets(patch: Path, *, strip: int, operation_id: str) -> list[Path]:
    """Return the bounded source-relative file set needed for a patch replay."""
    targets: set[Path] = set()
    for line in patch.read_text(encoding="utf-8").splitlines():
        if not (line.startswith("--- ") or line.startswith("+++ ")):
            continue
        raw = line[4:].split("\t", 1)[0].strip()
        if raw == "/dev/null":
            continue
        if raw.startswith('"'):
            try:
                decoded = shlex.split(raw, posix=True)
            except ValueError as exc:
                raise BuildToolError(
                    f"operation {operation_id}: invalid quoted patch path {raw!r}"
                ) from exc
            if len(decoded) != 1:
                raise BuildToolError(
                    f"operation {operation_id}: ambiguous quoted patch path {raw!r}"
                )
            raw = decoded[0]
        if "\\" in raw or "\x00" in raw:
            raise BuildToolError(f"operation {operation_id}: unsafe patch path {raw!r}")
        candidate = PurePosixPath(raw)
        parts = candidate.parts
        if candidate.is_absolute() or len(parts) <= strip:
            raise BuildToolError(f"operation {operation_id}: invalid patch path {raw!r}")
        relative_parts = parts[strip:]
        if any(part in {"", ".", ".."} for part in relative_parts):
            raise BuildToolError(f"operation {operation_id}: patch path escapes its cwd")
        targets.add(Path(*relative_parts))
    if not targets:
        raise BuildToolError(f"operation {operation_id}: fuzzy patch declares no file targets")
    return sorted(targets, key=lambda item: item.as_posix())


def _preflight_fuzzy_patch(
    command: list[str],
    *,
    patch: Path,
    cwd: Path,
    strip: int,
    operation_id: str,
    runner: CommandRunner,
) -> str:
    """Replay a fuzzy patch on only its touched files, without mutating sources.

    GNU patch's ``--dry-run`` does not materialize a new file, so it gives a
    false failure when a later diff in the same patch edits that new file. A
    bounded temporary replay preserves sequential semantics while copying no
    unrelated source content.
    """
    targets = _fuzzy_patch_targets(patch, strip=strip, operation_id=operation_id)
    source_root = cwd.resolve()
    with tempfile.TemporaryDirectory(prefix="op13-patch-preflight-") as temporary_name:
        sandbox = Path(temporary_name)
        for relative in targets:
            raw_source = cwd / relative
            if raw_source.is_symlink():
                raise BuildToolError(
                    f"operation {operation_id}: fuzzy patch target is a symlink: {relative}"
                )
            source = raw_source.resolve()
            try:
                source.relative_to(source_root)
            except ValueError as exc:
                raise BuildToolError(
                    f"operation {operation_id}: patch target escapes its cwd"
                ) from exc
            destination = sandbox / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source.exists():
                if not source.is_file():
                    raise BuildToolError(
                        f"operation {operation_id}: fuzzy patch target is not a file: {relative}"
                    )
                shutil.copy2(source, destination)
        replay = runner.run(command, cwd=sandbox, capture=True)
        residues = sorted(
            str(path.relative_to(sandbox))
            for pattern in ("*.rej", "*.orig")
            for path in sandbox.rglob(pattern)
            if path.is_file()
        )
        if residues:
            raise BuildToolError(
                f"operation {operation_id}: patch preflight left reject/backup files: {residues}"
            )
        return (replay.stdout or "") + (replay.stderr or "")


def _git_apply_output(result: subprocess.CompletedProcess[str]) -> str:
    return "\n".join(
        part.strip()
        for part in (result.stdout or "", result.stderr or "")
        if part.strip()
    )


def _reject_skipped_git_patch(operation_id: str, output: str) -> None:
    if re.search(r"\bSkipped patch\b", output, re.IGNORECASE):
        skipped = next(
            (
                line.strip()
                for line in output.splitlines()
                if re.search(r"\bSkipped patch\b", line, re.IGNORECASE)
            ),
            "Skipped patch",
        )
        raise BuildToolError(
            f"operation {operation_id}: git apply skipped a declared patch target: {skipped}"
        )


def _require_git_top(
    *,
    cwd: Path,
    operation_id: str,
    runner: CommandRunner,
) -> Path:
    result = runner.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=cwd,
        capture=True,
    )
    raw_top = (result.stdout or "").strip()
    if not raw_top:
        raise BuildToolError(
            f"operation {operation_id}: git apply directory has no repository top"
        )
    top = Path(raw_top).resolve()
    if top != cwd.resolve():
        raise BuildToolError(
            f"operation {operation_id}: git apply directory must run from the Git top "
            f"({top}), not {cwd.resolve()}"
        )
    return top


def _directory_patch_target_states(
    *,
    patch: Path,
    cwd: Path,
    directory: str,
    strip: int,
    operation_id: str,
) -> dict[str, str | None]:
    raw_base = cwd / directory
    if raw_base.is_symlink():
        raise BuildToolError(
            f"operation {operation_id}: patch directory must not be a symlink"
        )
    base = raw_base.resolve()
    cwd_root = cwd.resolve()
    try:
        base.relative_to(cwd_root)
    except ValueError as exc:
        raise BuildToolError(
            f"operation {operation_id}: patch directory escapes its cwd"
        ) from exc
    if not base.is_dir():
        raise BuildToolError(
            f"operation {operation_id}: patch directory is missing: {directory}"
        )

    states: dict[str, str | None] = {}
    for relative in _fuzzy_patch_targets(
        patch,
        strip=strip,
        operation_id=operation_id,
    ):
        raw_target = base / relative
        if raw_target.is_symlink():
            raise BuildToolError(
                f"operation {operation_id}: declared patch target is a symlink: "
                f"{(Path(directory) / relative).as_posix()}"
            )
        target = raw_target.resolve()
        try:
            target.relative_to(base)
        except ValueError as exc:
            raise BuildToolError(
                f"operation {operation_id}: declared patch target escapes its directory"
            ) from exc
        display = (Path(directory) / relative).as_posix()
        if not target.exists():
            states[display] = None
        elif not target.is_file():
            raise BuildToolError(
                f"operation {operation_id}: declared patch target is not a file: {display}"
            )
        else:
            states[display] = sha256_file(target)
    return states


def _execute_operation(
    operation: Mapping[str, Any],
    *,
    root: Path,
    source_dir: Path,
    cache_root: Path,
    lock: DependencyLock,
    base: str,
    root_variant: str,
    runner: CommandRunner,
    check_only: bool,
    smoke: bool,
) -> dict[str, Any]:
    operation_id = str(operation["id"])
    operation_type = str(operation["type"])
    record: dict[str, Any] = {"id": operation_id, "type": operation_type, "status": "checked" if check_only else "applied"}
    if "kernel_tree" in operation:
        record["kernel_tree"] = str(operation["kernel_tree"])
    if operation_type in {"apply", "git-apply"}:
        patch, patch_root = _operation_source(operation, root=root, cache_root=cache_root, lock=lock, base=base)
        cwd = _cwd(source_dir, operation)
        strip = operation.get("strip", 1)
        if not isinstance(strip, int) or isinstance(strip, bool) or strip < 0 or strip > 4:
            raise BuildToolError(f"operation {operation_id}: strip must be between 0 and 4")
        directory: str | None = None
        if "directory" in operation:
            directory = _safe_relative(
                operation["directory"],
                f"operation {operation_id}.directory",
            )
            if operation.get("fuzz", 0):
                raise BuildToolError(
                    f"operation {operation_id}: directory is incompatible with fuzzy patches"
                )
        if smoke:
            record.update({"path": str(patch), "sha256": None, "status": "smoke-checked"})
            if directory is not None:
                record["directory"] = directory
            return record
        if not patch.is_file():
            raise BuildToolError(f"operation {operation_id}: patch is missing: {patch}")
        if not cwd.is_dir():
            raise BuildToolError(f"operation {operation_id}: source cwd is missing: {cwd}")
        temporary: tempfile.TemporaryDirectory[str] | None = None
        patch_input = patch
        dependency_id = operation.get("dependency")
        try:
            if isinstance(dependency_id, str) and lock.dependencies[dependency_id].kind == "git":
                relative = patch.relative_to(patch_root)
                payload = _git_blob_bytes(patch_root, relative, operation_id)
                temporary = tempfile.TemporaryDirectory(prefix="op13-pinned-patch-")
                patch_input = Path(temporary.name) / patch.name
                patch_input.write_bytes(payload)
                actual_sha256 = hashlib.sha256(payload).hexdigest()
            else:
                actual_sha256 = sha256_file(patch)
            expected_sha256 = operation.get("sha256")
            if expected_sha256 is not None and actual_sha256 != expected_sha256:
                raise BuildToolError(
                    f"operation {operation_id}: patch digest mismatch: "
                    f"expected {expected_sha256}, got {actual_sha256}"
                )
            fuzz = operation.get("fuzz", 0)
            if fuzz:
                residues_before = {
                    path.resolve()
                    for pattern in ("*.rej", "*.orig")
                    for path in cwd.rglob(pattern)
                    if path.is_file()
                }
                command = [
                    _patch_utility(),
                    "--batch",
                    "--forward",
                    f"--fuzz={fuzz}",
                    "--no-backup-if-mismatch",
                    "--reject-file=-",
                    f"-p{strip}",
                    f"--input={patch_input}",
                ]
                preflight_output = _preflight_fuzzy_patch(
                    command,
                    patch=patch_input,
                    cwd=cwd,
                    strip=strip,
                    operation_id=operation_id,
                    runner=runner,
                )
                outputs = ["preflight replay:\n" + preflight_output]
                if not check_only:
                    applied = runner.run(command, cwd=cwd, capture=True)
                    outputs.append((applied.stdout or "") + (applied.stderr or ""))
                residues_after = {
                    path.resolve()
                    for pattern in ("*.rej", "*.orig")
                    for path in cwd.rglob(pattern)
                    if path.is_file()
                }
                new_residues = sorted(str(path) for path in residues_after - residues_before)
                if new_residues:
                    raise BuildToolError(
                        f"operation {operation_id}: patch utility left reject/backup files: {new_residues}"
                    )
                patch_output = "\n".join(part.strip() for part in outputs if part.strip())
                if re.search(r"\bFAILED\b|saving rejects", patch_output, re.IGNORECASE):
                    raise BuildToolError(f"operation {operation_id}: patch output reported a rejected hunk")
                record.update({"fuzz": fuzz, "patch_output": patch_output})
            else:
                declared_before: dict[str, str | None] | None = None
                if directory is not None:
                    _require_git_top(
                        cwd=cwd,
                        operation_id=operation_id,
                        runner=runner,
                    )
                    declared_before = _directory_patch_target_states(
                        patch=patch_input,
                        cwd=cwd,
                        directory=directory,
                        strip=strip,
                        operation_id=operation_id,
                    )
                git_apply = [
                    "git",
                    "-c",
                    "core.autocrlf=false",
                    "-c",
                    "core.eol=lf",
                    "apply",
                    "--verbose",
                ]
                if directory is not None:
                    git_apply.append(f"--directory={Path(directory).as_posix()}")
                checked = runner.run(
                    [*git_apply, "--check", f"-p{strip}", str(patch_input)],
                    cwd=cwd,
                    capture=True,
                )
                outputs = [_git_apply_output(checked)]
                _reject_skipped_git_patch(operation_id, outputs[0])
                if not check_only:
                    applied = runner.run(
                        [*git_apply, f"-p{strip}", str(patch_input)],
                        cwd=cwd,
                        capture=True,
                    )
                    applied_output = _git_apply_output(applied)
                    outputs.append(applied_output)
                    _reject_skipped_git_patch(operation_id, applied_output)
                    if declared_before is not None:
                        declared_after = _directory_patch_target_states(
                            patch=patch_input,
                            cwd=cwd,
                            directory=directory,
                            strip=strip,
                            operation_id=operation_id,
                        )
                        unchanged = sorted(
                            path
                            for path, before in declared_before.items()
                            if declared_after.get(path) == before
                        )
                        if unchanged:
                            raise BuildToolError(
                                f"operation {operation_id}: git apply left declared patch "
                                f"targets unchanged: {unchanged}"
                            )
                        record.update(
                            {
                                "declared_targets": sorted(declared_before),
                                "pre_sha256": declared_before,
                                "post_sha256": declared_after,
                            }
                        )
                patch_output = "\n".join(output for output in outputs if output)
                if patch_output:
                    record["patch_output"] = patch_output
                if directory is not None:
                    record["directory"] = directory
            record.update({"path": str(patch), "sha256": actual_sha256, "cwd": str(cwd), "strip": strip})
            return record
        finally:
            if temporary is not None:
                temporary.cleanup()

    if operation_type == "copy":
        source, _ = _operation_source(operation, root=root, cache_root=cache_root, lock=lock, base=base)
        cwd = _cwd(source_dir, operation)
        destination_value = _safe_relative(operation.get("destination"), f"operation {operation_id}.destination")
        destination = (cwd / destination_value).resolve()
        try:
            destination.relative_to(source_dir.resolve())
        except ValueError as exc:
            raise BuildToolError(f"operation {operation_id}: copy destination escapes source tree") from exc
        overwrite = operation.get("overwrite", False)
        if not isinstance(overwrite, bool):
            raise BuildToolError(f"operation {operation_id}: overwrite must be boolean")
        if smoke:
            record.update({"source": str(source), "destination": str(destination), "status": "smoke-checked"})
            return record
        if not source.exists():
            raise BuildToolError(f"operation {operation_id}: copy source is missing: {source}")
        if destination.exists() and not overwrite:
            raise BuildToolError(f"operation {operation_id}: destination already exists: {destination}")
        if not check_only:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                if overwrite:
                    raise BuildToolError(f"operation {operation_id}: tree overwrite is forbidden")
                _copy_tree_strict(source, destination)
            else:
                if source.is_symlink():
                    raise BuildToolError(f"operation {operation_id}: symlink copy is forbidden")
                shutil.copy2(source, destination)
        record.update({"source": str(source), "destination": str(destination)})
        if source.is_file():
            record["sha256"] = sha256_file(source)
        return record

    if operation_type == "replace":
        target = _source_target(source_dir, operation, "target")
        find = operation.get("find")
        replacement = operation.get("replace")
        count = operation.get("count")
        if not isinstance(find, str) or not find or not isinstance(replacement, str):
            raise BuildToolError(f"operation {operation_id}: find/replace must be strings")
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            raise BuildToolError(f"operation {operation_id}: count must be a positive integer")
        if smoke:
            record.update({"target": str(target), "status": "smoke-checked"})
            return record
        if not target.is_file():
            raise BuildToolError(f"operation {operation_id}: edit target is missing: {target}")
        text = target.read_text(encoding="utf-8")
        actual_count = text.count(find)
        if actual_count != count:
            raise BuildToolError(
                f"operation {operation_id}: expected {count} occurrences in {target}, found {actual_count}"
            )
        before = sha256_file(target)
        if not check_only:
            target.write_text(text.replace(find, replacement), encoding="utf-8", newline="\n")
        record.update({"target": str(target), "before_sha256": before, "count": count})
        if not check_only:
            record["after_sha256"] = sha256_file(target)
        return record

    if operation_type == "append":
        target = _source_target(source_dir, operation, "target")
        lines = _as_string_list(operation.get("lines"), f"operation {operation_id}.lines")
        if smoke:
            record.update({"target": str(target), "lines": lines, "status": "smoke-checked"})
            return record
        if not target.is_file():
            raise BuildToolError(f"operation {operation_id}: append target is missing: {target}")
        text = target.read_text(encoding="utf-8")
        duplicates = [line for line in lines if line in text.splitlines()]
        if duplicates:
            raise BuildToolError(f"operation {operation_id}: lines already present: {duplicates}")
        before = sha256_file(target)
        if not check_only:
            separator = "" if text.endswith("\n") or not text else "\n"
            target.write_text(text + separator + "\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        record.update({"target": str(target), "before_sha256": before, "lines": lines})
        if not check_only:
            record["after_sha256"] = sha256_file(target)
        return record

    if operation_type == "exec":
        argv = _as_string_list(operation.get("argv"), f"operation {operation_id}.argv")
        expected_outputs = _as_string_list(operation.get("expected_outputs"), f"operation {operation_id}.expected_outputs")
        if not expected_outputs:
            raise BuildToolError(f"operation {operation_id}: exec must declare expected_outputs")
        dependency_id = operation.get("dependency")
        declared_dependencies = set(_as_string_list(operation.get("dependencies", []), f"operation {operation_id}.dependencies"))
        if isinstance(dependency_id, str):
            declared_dependencies.add(dependency_id)
        _validate_exec_argv_placeholders(
            argv,
            declared_dependencies,
            f"operation {operation_id}.argv",
        )
        dependency_dir = _dependency_dir(cache_root, lock, dependency_id) if isinstance(dependency_id, str) else root.resolve()
        replacements = {
            "{source_dir}": str(source_dir.resolve()),
            "{cache_root}": str(cache_root.resolve()),
            "{dependency_dir}": str(dependency_dir),
            "{repo_root}": str(root.resolve()),
            "{base}": base,
            "{root_variant}": root_variant,
        }
        for declared in declared_dependencies:
            if declared not in lock.dependencies:
                raise BuildToolError(f"operation {operation_id}: unknown dependency placeholder {declared!r}")
            replacements[f"{{dependency_dir:{declared}}}"] = str(_dependency_dir(cache_root, lock, declared))
            if not smoke:
                _verify_dependency_checkout(cache_root, lock, declared, runner)
        expanded: list[str] = []
        for token in argv:
            for match in re.findall(r"\{dependency_dir:([^{}]+)\}", token):
                if match not in declared_dependencies:
                    raise BuildToolError(
                        f"operation {operation_id}: dependency placeholder {match!r} is not declared"
                    )
            for marker, value in replacements.items():
                token = token.replace(marker, value)
            if "{dependency_dir:" in token or "{" in token or "}" in token:
                raise BuildToolError(f"operation {operation_id}: unresolved argv placeholder {token!r}")
            if "http://" in token or "https://" in token:
                raise BuildToolError(f"operation {operation_id}: network arguments are forbidden")
            expanded.append(token)
        executable = Path(expanded[0]).resolve()
        dependency_roots = tuple(_dependency_dir(cache_root, lock, item) for item in declared_dependencies)
        allowed_roots = (source_dir.resolve(), dependency_dir.resolve(), root.resolve(), *dependency_roots)
        if Path(expanded[0]).name in {"python", "python3"}:
            if len(expanded) < 2:
                raise BuildToolError(f"operation {operation_id}: Python exec needs a script path")
            script_path = Path(expanded[1]).resolve()
            if not any(_is_inside(script_path, allowed) for allowed in allowed_roots):
                raise BuildToolError(f"operation {operation_id}: Python script is not from a pinned/local tree")
            expanded[0] = sys.executable
            executable = Path(sys.executable).resolve()
        elif not any(_is_inside(executable, allowed) for allowed in allowed_roots):
            raise BuildToolError(f"operation {operation_id}: executable is not from a pinned/local tree")
        if any(token == "-c" for token in expanded[1:]):
            raise BuildToolError(f"operation {operation_id}: shell command strings are forbidden")
        cwd = _cwd(source_dir, operation)
        outputs = [_source_target(source_dir, {**operation, "output": value}, "output") for value in expected_outputs]
        if not smoke and not check_only:
            if not executable.is_file():
                raise BuildToolError(f"operation {operation_id}: executable is missing: {executable}")
            runner.run(expanded, cwd=cwd)
            missing = [str(path) for path in outputs if not path.exists()]
            if missing:
                raise BuildToolError(f"operation {operation_id}: expected outputs missing: {missing}")
        record.update({"argv": expanded, "expected_outputs": [str(path) for path in outputs]})
        if smoke:
            record["status"] = "smoke-checked"
        return record

    raise AssertionError(operation_type)


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _write_patch_operations_log(path: Path, document: Mapping[str, Any]) -> None:
    """Atomically persist patch progress so a failing operation remains diagnosable."""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, indent=2, sort_keys=True) + "\n"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            stream.write(payload)
            temporary_path = Path(stream.name)
        temporary_path.replace(path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _bounded_failure_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "\n... failure evidence truncated"


def _exception_evidence(exc: Exception) -> dict[str, str]:
    evidence = {
        "reason": _bounded_failure_text(str(exc), FAILURE_REASON_LIMIT),
        "error_type": type(exc).__name__,
    }
    if not isinstance(exc, BuildToolError):
        evidence["traceback"] = _bounded_failure_text(
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            FAILURE_TRACEBACK_LIMIT,
        )
    return evidence


def apply_patch_series(
    *,
    root: Path,
    source_dir: Path,
    cache_root: Path,
    context_path: Path,
    profile: Profile,
    feature: FeatureProfile,
    lock: DependencyLock,
    root_variant: str,
    check_only: bool,
    smoke: bool,
    log_dir: Path,
) -> list[dict[str, Any]]:
    if root_variant not in ROOT_VARIANTS:
        raise BuildToolError(f"unsupported root variant {root_variant!r}")
    context = load_context(context_path)
    validate_lineage(context, profile, lock, minimum_stage="sources-synced")
    if bool(context.get("smoke")) != bool(smoke):
        raise BuildToolError("smoke and real build contexts must never be mixed")
    paths = _series_paths(root, feature, root_variant)
    series: list[tuple[str, list[dict[str, Any]]]] = [_load_series(path) for path in paths]
    operation_ids: set[str] = set()
    expanded_operation_ids: set[str] = set()
    selected_operations: list[dict[str, Any]] = []
    for series_id, operations in series:
        for operation in operations:
            qualified = f"{series_id}:{operation['id']}"
            if qualified in operation_ids:
                raise BuildToolError(f"duplicate qualified patch operation {qualified}")
            operation_ids.add(qualified)
            if _operation_enabled(operation, feature, profile.id, root_variant):
                operation = dict(operation)
                operation["id"] = qualified
                for expanded in _expand_kernel_tree_operation(
                    operation,
                    f"patch operation {qualified}",
                ):
                    expanded_id = str(expanded["id"])
                    if expanded_id in expanded_operation_ids:
                        raise BuildToolError(
                            f"duplicate expanded patch operation {expanded_id}"
                        )
                    expanded_operation_ids.add(expanded_id)
                    selected_operations.append(expanded)
    dependency_set: set[str] = set()
    for operation in selected_operations:
        if operation.get("dependency") is not None:
            dependency_set.add(str(operation["dependency"]))
        dependency_set.update(_as_string_list(operation.get("dependencies", []), f"operation {operation['id']}.dependencies"))
    dependency_ids = sorted(dependency_set)
    for dependency_id in dependency_ids:
        if dependency_id not in lock.dependencies:
            raise BuildToolError(f"patch operation references unlocked dependency {dependency_id}")
    records: list[dict[str, Any]] = []
    log_path = log_dir / "patch-operations.json"
    log_document: dict[str, Any] = {
        "schema_version": 2,
        "status": "in-progress",
        "profile": profile.id,
        "feature_profile": feature.id,
        "root_variant": root_variant,
        "check_only": check_only,
        "smoke": smoke,
        "operations": records,
    }
    _write_patch_operations_log(log_path, log_document)
    if dependency_ids and not smoke:
        try:
            fetch_dependencies(lock, cache_root, selected=dependency_ids, dry_run=False, offline=False)
        except Exception as exc:
            failure = {
                "stage": "dependency-fetch",
                "status": "failed",
                **_exception_evidence(exc),
                "dependencies": dependency_ids,
            }
            log_document.update({"status": "failed", "failure": failure})
            _write_patch_operations_log(log_path, log_document)
            if isinstance(exc, BuildToolError):
                raise
            raise BuildToolError(f"patch dependency fetch failed: {exc}") from exc
    runner = CommandRunner(dry_run=False)
    for operation in selected_operations:
        current_operation = {
            "id": operation["id"],
            "type": operation["type"],
            "status": "started",
        }
        if "kernel_tree" in operation:
            current_operation["kernel_tree"] = operation["kernel_tree"]
        log_document["current_operation"] = current_operation
        _write_patch_operations_log(log_path, log_document)
        try:
            record = _execute_operation(
                operation,
                root=root,
                source_dir=source_dir,
                cache_root=cache_root,
                lock=lock,
                base=profile.id,
                root_variant=root_variant,
                runner=runner,
                check_only=check_only,
                smoke=smoke,
            )
        except Exception as exc:
            failure = {
                "id": operation["id"],
                "type": operation["type"],
                "status": "failed",
                **_exception_evidence(exc),
            }
            if "kernel_tree" in operation:
                failure["kernel_tree"] = operation["kernel_tree"]
            if operation["optional"] and isinstance(exc, BuildToolError):
                skipped = {**failure, "status": "optional-skipped"}
                records.append(skipped)
                log_document.pop("current_operation", None)
                _write_patch_operations_log(log_path, log_document)
                continue
            records.append(failure)
            log_document.update(
                {
                    "status": "failed",
                    "failure": failure,
                    "current_operation": failure,
                }
            )
            _write_patch_operations_log(log_path, log_document)
            if isinstance(exc, BuildToolError):
                raise
            raise BuildToolError(
                f"operation {operation['id']}: patch operation failed: {exc}"
            ) from exc
        else:
            records.append(record)
            log_document.pop("current_operation", None)
            _write_patch_operations_log(log_path, log_document)
    log_document["status"] = "operations-completed"
    _write_patch_operations_log(log_path, log_document)
    if not check_only:
        try:
            updated = advance_context(
                context,
                "patches-applied",
                {
                    "features": [feature_selection(feature, root_variant)],
                    "patches": records,
                },
            )
            write_context(context_path, updated)
        except Exception as exc:
            failure = {
                "stage": "context-update",
                "status": "failed",
                **_exception_evidence(exc),
            }
            log_document.update({"status": "failed", "failure": failure})
            _write_patch_operations_log(log_path, log_document)
            if isinstance(exc, BuildToolError):
                raise
            raise BuildToolError(f"patch context update failed: {exc}") from exc
    log_document["status"] = "completed"
    _write_patch_operations_log(log_path, log_document)
    return records


def validate_series_documents(
    root: Path,
    profiles: Mapping[str, Profile],
    features: Mapping[str, FeatureProfile],
    lock: DependencyLock,
) -> dict[str, Any]:
    loaded: dict[Path, tuple[str, list[dict[str, Any]]]] = {}
    combinations = 0
    operation_count = 0
    for feature in features.values():
        for root_variant in (*feature.root_variants, "none"):
            paths = _series_paths(root, feature, root_variant)
            for path in paths:
                loaded.setdefault(path, _load_series(path))
            for profile in profiles.values():
                combinations += 1
                expanded_operation_ids: set[str] = set()
                for path in paths:
                    series_id, operations = loaded[path]
                    for logical_operation in operations:
                        if not _operation_enabled(
                            logical_operation,
                            feature,
                            profile.id,
                            root_variant,
                        ):
                            continue
                        qualified = dict(logical_operation)
                        qualified["id"] = f"{series_id}:{logical_operation['id']}"
                        for operation in _expand_kernel_tree_operation(
                            qualified,
                            f"{path}: operation {qualified['id']}",
                        ):
                            operation_id = str(operation["id"])
                            if operation_id in expanded_operation_ids:
                                raise BuildToolError(
                                    f"{path}: duplicate expanded patch operation {operation_id}"
                                )
                            expanded_operation_ids.add(operation_id)
                            operation_count += 1
                            dependency_id = operation.get("dependency")
                            if dependency_id is not None and dependency_id not in lock.dependencies:
                                raise BuildToolError(
                                    f"{path}: operation {operation_id} references unlocked dependency {dependency_id}"
                                )
                            for extra_dependency in _as_string_list(
                                operation.get("dependencies", []),
                                f"{path}:{operation_id}.dependencies",
                            ):
                                if extra_dependency not in lock.dependencies:
                                    raise BuildToolError(
                                        f"{path}: operation {operation_id} references unlocked dependency {extra_dependency}"
                                    )
                            if "cwd" in operation:
                                _safe_relative(operation["cwd"], f"{path}:{operation_id}.cwd")
                            if "directory" in operation:
                                _safe_relative(
                                    operation["directory"],
                                    f"{path}:{operation_id}.directory",
                                )
                            if operation["type"] in {"apply", "git-apply", "copy"}:
                                selected_path = operation.get("path")
                                if "path_by_base" in operation:
                                    mapping = operation["path_by_base"]
                                    if not isinstance(mapping, dict) or profile.id not in mapping:
                                        if not operation["optional"]:
                                            raise BuildToolError(
                                                f"{path}: operation {operation_id} lacks a path for {profile.id}"
                                            )
                                        continue
                                    selected_path = mapping[profile.id]
                                relative = _safe_relative(selected_path, f"{path}:{operation_id}.path")
                                if dependency_id is None:
                                    resolve_inside(
                                        root,
                                        relative,
                                        f"{path}:{operation_id}.path",
                                        must_exist=not operation["optional"],
                                    )
                            if operation["type"] == "copy":
                                _safe_relative(
                                    operation.get("destination"),
                                    f"{path}:{operation_id}.destination",
                                )
                            if operation["type"] in {"replace", "append"}:
                                _safe_relative(
                                    operation.get("target"),
                                    f"{path}:{operation_id}.target",
                                )
                            if operation["type"] == "exec":
                                argv = _as_string_list(
                                    operation.get("argv"),
                                    f"{path}:{operation_id}.argv",
                                )
                                declared_dependencies = set(
                                    _as_string_list(
                                        operation.get("dependencies", []),
                                        f"{path}:{operation_id}.dependencies",
                                    )
                                )
                                if isinstance(dependency_id, str):
                                    declared_dependencies.add(dependency_id)
                                _validate_exec_argv_placeholders(
                                    argv,
                                    declared_dependencies,
                                    f"{path}:{operation_id}.argv",
                                )
                                outputs = _as_string_list(
                                    operation.get("expected_outputs"),
                                    f"{path}:{operation_id}.expected_outputs",
                                )
                                if not outputs:
                                    raise BuildToolError(
                                        f"{path}: exec operation {operation_id} needs expected_outputs"
                                    )
                                for output in outputs:
                                    _safe_relative(output, f"{path}:{operation_id}.expected_outputs")
    return {
        "series": sorted(series_id for series_id, _ in loaded.values()),
        "combinations": combinations,
        "enabled_operations": operation_count,
    }
