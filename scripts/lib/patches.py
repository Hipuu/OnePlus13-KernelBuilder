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
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

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
        if "sha256" in operation:
            expected_sha256 = operation["sha256"]
            if operation_type not in {"apply", "git-apply"}:
                raise BuildToolError(f"{where}: sha256 is supported only for patch operations")
            if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
                raise BuildToolError(f"{where}: sha256 must be a lowercase SHA-256 digest")
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
    if operation_type in {"apply", "git-apply"}:
        patch, patch_root = _operation_source(operation, root=root, cache_root=cache_root, lock=lock, base=base)
        cwd = _cwd(source_dir, operation)
        strip = operation.get("strip", 1)
        if not isinstance(strip, int) or isinstance(strip, bool) or strip < 0 or strip > 4:
            raise BuildToolError(f"operation {operation_id}: strip must be between 0 and 4")
        if smoke:
            record.update({"path": str(patch), "sha256": None, "status": "smoke-checked"})
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
                git_apply = ["git", "-c", "core.autocrlf=false", "-c", "core.eol=lf", "apply"]
                runner.run([*git_apply, "--check", f"-p{strip}", str(patch_input)], cwd=cwd)
                if not check_only:
                    runner.run([*git_apply, f"-p{strip}", str(patch_input)], cwd=cwd)
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
        dependency_dir = _dependency_dir(cache_root, lock, dependency_id) if isinstance(dependency_id, str) else root.resolve()
        replacements = {
            "{source_dir}": str(source_dir.resolve()),
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
                selected_operations.append(operation)
    dependency_set: set[str] = set()
    for operation in selected_operations:
        if operation.get("dependency") is not None:
            dependency_set.add(str(operation["dependency"]))
        dependency_set.update(_as_string_list(operation.get("dependencies", []), f"operation {operation['id']}.dependencies"))
    dependency_ids = sorted(dependency_set)
    for dependency_id in dependency_ids:
        if dependency_id not in lock.dependencies:
            raise BuildToolError(f"patch operation references unlocked dependency {dependency_id}")
    if dependency_ids and not smoke:
        fetch_dependencies(lock, cache_root, selected=dependency_ids, dry_run=False, offline=False)
    runner = CommandRunner(dry_run=False)
    records: list[dict[str, Any]] = []
    for operation in selected_operations:
        try:
            records.append(
                _execute_operation(
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
            )
        except BuildToolError as exc:
            if operation["optional"]:
                records.append(
                    {"id": operation["id"], "type": operation["type"], "status": "optional-skipped", "reason": str(exc)}
                )
                continue
            raise
    log_dir.mkdir(parents=True, exist_ok=True)
    log_document = {
        "schema_version": 1,
        "profile": profile.id,
        "feature_profile": feature.id,
        "root_variant": root_variant,
        "check_only": check_only,
        "smoke": smoke,
        "operations": records,
    }
    (log_dir / "patch-operations.json").write_text(
        json.dumps(log_document, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    if not check_only:
        updated = advance_context(
            context,
            "patches-applied",
            {
                "features": [feature_selection(feature, root_variant)],
                "patches": records,
            },
        )
        write_context(context_path, updated)
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
                for path in paths:
                    _, operations = loaded[path]
                    for operation in operations:
                        if not _operation_enabled(operation, feature, profile.id, root_variant):
                            continue
                        operation_count += 1
                        dependency_id = operation.get("dependency")
                        if dependency_id is not None and dependency_id not in lock.dependencies:
                            raise BuildToolError(
                                f"{path}: operation {operation['id']} references unlocked dependency {dependency_id}"
                            )
                        for extra_dependency in _as_string_list(
                            operation.get("dependencies", []), f"{path}:{operation['id']}.dependencies"
                        ):
                            if extra_dependency not in lock.dependencies:
                                raise BuildToolError(
                                    f"{path}: operation {operation['id']} references unlocked dependency {extra_dependency}"
                                )
                        if operation["type"] in {"apply", "git-apply", "copy"}:
                            selected_path = operation.get("path")
                            if "path_by_base" in operation:
                                mapping = operation["path_by_base"]
                                if not isinstance(mapping, dict) or profile.id not in mapping:
                                    if not operation["optional"]:
                                        raise BuildToolError(
                                            f"{path}: operation {operation['id']} lacks a path for {profile.id}"
                                        )
                                    continue
                                selected_path = mapping[profile.id]
                            relative = _safe_relative(selected_path, f"{path}:{operation['id']}.path")
                            if dependency_id is None:
                                resolve_inside(root, relative, f"{path}:{operation['id']}.path", must_exist=not operation["optional"])
                        if operation["type"] in {"replace", "append"}:
                            _safe_relative(operation.get("target"), f"{path}:{operation['id']}.target")
                        if operation["type"] == "exec":
                            _as_string_list(operation.get("argv"), f"{path}:{operation['id']}.argv")
                            outputs = _as_string_list(
                                operation.get("expected_outputs"), f"{path}:{operation['id']}.expected_outputs"
                            )
                            if not outputs:
                                raise BuildToolError(f"{path}: exec operation {operation['id']} needs expected_outputs")
    return {
        "series": sorted(series_id for series_id, _ in loaded.values()),
        "combinations": combinations,
        "enabled_operations": operation_count,
    }
