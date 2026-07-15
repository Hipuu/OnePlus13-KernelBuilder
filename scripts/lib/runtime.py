"""Network, command, and source synchronization primitives."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

from .config import (
    Dependency,
    DependencyLock,
    Profile,
    is_full_commit,
    sha256_file,
)
from .context import atomic_write_json, new_context, write_context
from .errors import BuildToolError, SourceChanged


REPO_INIT_STORAGE_FLAGS = ("--depth=1", "--no-tags", "--no-clone-bundle")
REPO_SYNC_STORAGE_FLAGS = (
    "--current-branch",
    "--detach",
    "--no-tags",
    "--no-clone-bundle",
    "--optimized-fetch",
    "--prune",
)


class CommandRunner:
    def __init__(self, *, dry_run: bool = False, verbose: bool = True) -> None:
        self.dry_run = dry_run
        self.verbose = verbose
        self.commands: list[list[str]] = []

    @staticmethod
    def _display(argv: Sequence[str]) -> str:
        def quote(value: str) -> str:
            if value and all(ch.isalnum() or ch in "_./:=+@,-" for ch in value):
                return value
            return json.dumps(value)

        return " ".join(quote(str(value)) for value in argv)

    def run(
        self,
        argv: Sequence[str | os.PathLike[str]],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        capture: bool = False,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        command = [str(value) for value in argv]
        if not command:
            raise BuildToolError("empty command")
        self.commands.append(command)
        if self.verbose:
            prefix = f"[{cwd}] " if cwd else ""
            print(f"+ {prefix}{self._display(command)}", flush=True)
        if self.dry_run:
            return subprocess.CompletedProcess(command, 0, "", "")
        merged_env = os.environ.copy()
        if env:
            merged_env.update({str(key): str(value) for key, value in env.items()})
        try:
            return subprocess.run(
                command,
                cwd=cwd,
                env=merged_env,
                check=check,
                text=True,
                stdout=subprocess.PIPE if capture else None,
                stderr=subprocess.PIPE if capture else None,
            )
        except FileNotFoundError as exc:
            raise BuildToolError(f"required command not found: {command[0]}") from exc
        except subprocess.CalledProcessError as exc:
            captured = [part.strip() for part in (exc.stdout, exc.stderr) if part and part.strip()]
            combined = "\n".join(captured)
            if len(combined) > 8000:
                combined = combined[:8000] + "\n... output truncated"
            detail = f": {combined}" if combined else ""
            raise BuildToolError(f"command failed ({exc.returncode}): {self._display(command)}{detail}") from exc


def _safe_cache_path(cache_root: Path, *parts: str) -> Path:
    result = cache_root.joinpath(*parts).resolve()
    try:
        result.relative_to(cache_root.resolve())
    except ValueError as exc:
        raise BuildToolError("dependency cache path escaped its root") from exc
    return result


def _suffix_from_url(url: str) -> str:
    name = Path(urlsplit(url).path).name
    suffixes = Path(name).suffixes
    return "".join(suffixes[-2:]) if len(suffixes) >= 2 and suffixes[-2] == ".tar" else (suffixes[-1] if suffixes else "")


def _download_verified(dependency: Dependency, destination: Path, *, offline: bool) -> Path:
    if destination.is_file():
        if sha256_file(destination) != dependency.sha256:
            raise BuildToolError(f"cached file digest mismatch for {dependency.id}: {destination}")
        return destination
    if offline:
        raise BuildToolError(f"offline cache miss for {dependency.id}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{dependency.id}.", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            request = urllib.request.Request(
                dependency.url,
                headers={"User-Agent": "OnePlus13-KernelBuilder/1"},
            )
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    shutil.copyfileobj(response, output, length=1024 * 1024)
            except (urllib.error.URLError, TimeoutError) as exc:
                raise BuildToolError(f"download failed for {dependency.id}: {exc}") from exc
            output.flush()
            os.fsync(output.fileno())
        actual = sha256_file(temporary)
        if actual != dependency.sha256:
            raise BuildToolError(
                f"download digest mismatch for {dependency.id}: expected {dependency.sha256}, got {actual}"
            )
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _verify_git_checkout(path: Path, dependency: Dependency, runner: CommandRunner) -> None:
    if not (path / ".git").exists():
        raise BuildToolError(f"dependency cache is not a Git checkout: {path}")
    head = runner.run(["git", "rev-parse", "HEAD"], cwd=path, capture=True).stdout.strip()
    if head != dependency.commit:
        raise BuildToolError(
            f"cached Git dependency {dependency.id} is at {head}, expected {dependency.commit}"
        )
    remote = runner.run(["git", "remote", "get-url", "origin"], cwd=path, capture=True).stdout.strip()
    if remote.rstrip("/") != dependency.url.rstrip("/"):
        raise BuildToolError(f"cached Git dependency {dependency.id} has an unexpected origin")
    status = runner.run(
        [
            "git",
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignored=matching",
        ],
        cwd=path,
        capture=True,
    ).stdout
    if status.strip():
        raise BuildToolError(
            f"cached Git dependency {dependency.id} contains modified, untracked, or ignored files"
        )
    staged = runner.run(["git", "ls-files", "--stage"], cwd=path, capture=True).stdout
    if any(line.startswith("160000 ") for line in staged.splitlines()):
        raise BuildToolError(
            f"cached Git dependency {dependency.id} contains unsupported Git submodules"
        )


def _fetch_git(dependency: Dependency, destination: Path, runner: CommandRunner, *, offline: bool) -> Path:
    if destination.exists():
        _verify_git_checkout(destination, dependency, runner)
        return destination
    if offline:
        raise BuildToolError(f"offline cache miss for {dependency.id}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = _safe_cache_path(destination.parent, f".{dependency.id}.tmp")
    if temporary.exists():
        raise BuildToolError(f"stale dependency temporary directory: {temporary}")
    if runner.dry_run:
        runner.run(["git", "init", str(temporary)])
        runner.run(["git", "-C", str(temporary), "remote", "add", "origin", dependency.url])
        runner.run(["git", "-C", str(temporary), "fetch", "--depth=1", "origin", str(dependency.commit)])
        runner.run(["git", "-C", str(temporary), "checkout", "--detach", str(dependency.commit)])
        return destination
    try:
        runner.run(["git", "init", str(temporary)])
        runner.run(["git", "-C", str(temporary), "remote", "add", "origin", dependency.url])
        runner.run(["git", "-C", str(temporary), "fetch", "--depth=1", "--no-tags", "origin", str(dependency.commit)])
        runner.run(["git", "-C", str(temporary), "checkout", "--detach", str(dependency.commit)])
        _verify_git_checkout(temporary, dependency, runner)
        temporary.replace(destination)
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return destination


def fetch_dependencies(
    lock: DependencyLock,
    cache_root: Path,
    *,
    selected: Iterable[str] | None = None,
    dry_run: bool = False,
    offline: bool = False,
) -> dict[str, Any]:
    cache_root = cache_root.resolve()
    cache_root.mkdir(parents=True, exist_ok=True)
    runner = CommandRunner(dry_run=dry_run)
    wanted = sorted(set(selected) if selected is not None else lock.dependencies)
    unknown = sorted(set(wanted) - set(lock.dependencies))
    if unknown:
        raise BuildToolError(f"unknown dependencies: {', '.join(unknown)}")
    records: dict[str, Any] = {}
    for dependency_id in wanted:
        dependency = lock.dependencies[dependency_id]
        if dependency.kind == "git":
            destination = _safe_cache_path(cache_root, "git", dependency.id)
            fetched = _fetch_git(dependency, destination, runner, offline=offline)
            records[dependency.id] = {
                "kind": dependency.kind,
                "path": str(fetched),
                "commit": dependency.commit,
            }
        else:
            suffix = _suffix_from_url(dependency.url)
            destination = _safe_cache_path(cache_root, "files", f"{dependency.id}-{dependency.sha256[:12]}{suffix}")
            if dry_run:
                print(f"+ download {dependency.url} -> {destination}")
                fetched = destination
            else:
                fetched = _download_verified(dependency, destination, offline=offline)
            records[dependency.id] = {
                "kind": dependency.kind,
                "path": str(fetched),
                "sha256": dependency.sha256,
            }
    state = {
        "schema_version": 1,
        "dependency_lock_sha256": lock.digest,
        "dependencies": records,
        "dry_run": dry_run,
    }
    if not dry_run:
        atomic_write_json(cache_root / "dependency-state.json", state)
    return state


def _repo_launcher_path(cache_root: Path, lock: DependencyLock) -> Path:
    dependency = lock.dependencies["repo_launcher"]
    suffix = _suffix_from_url(dependency.url)
    return _safe_cache_path(cache_root.resolve(), "files", f"repo_launcher-{dependency.sha256[:12]}{suffix}")


def _repo_implementation_pin(lock: DependencyLock) -> tuple[str, str]:
    raw = lock.dependencies["repo_launcher"].raw
    url = raw.get("repo_url")
    commit = raw.get("repo_commit")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise BuildToolError("repo_launcher must declare an HTTPS repo_url")
    if not is_full_commit(commit):
        raise BuildToolError("repo_launcher must declare a full repo_commit")
    return url, commit


def validate_resolved_manifest(path: Path) -> int:
    try:
        tree = ET.parse(path)
    except (OSError, ET.ParseError) as exc:
        raise BuildToolError(f"invalid resolved repo manifest {path}: {exc}") from exc
    projects = tree.findall(".//project")
    if not projects:
        raise BuildToolError(f"resolved repo manifest has no projects: {path}")
    seen_paths: set[str] = set()
    for project in projects:
        name = project.get("name")
        checkout_path = project.get("path") or name
        revision = project.get("revision")
        if not name or not checkout_path:
            raise BuildToolError("resolved manifest project is missing name/path")
        if checkout_path in seen_paths:
            raise BuildToolError(f"resolved manifest repeats checkout path {checkout_path}")
        seen_paths.add(checkout_path)
        if not is_full_commit(revision):
            raise BuildToolError(f"resolved manifest project {name} is not pinned to a full commit")
    return len(projects)


def _manifest_projects(path: Path) -> dict[tuple[str, str], str]:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise BuildToolError(f"invalid manifest {path}: {exc}") from exc
    result: dict[tuple[str, str], str] = {}
    for project in root.findall("project"):
        name = project.get("name")
        checkout_path = project.get("path") or name
        revision = project.get("revision")
        if name and checkout_path and revision:
            result[(name, checkout_path)] = revision
    return result


def assert_manifest_matches_lock(resolved: Path, locked: Path) -> None:
    actual = _manifest_projects(resolved)
    expected = _manifest_projects(locked)
    if actual != expected:
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        changed = sorted(key for key in set(actual) & set(expected) if actual[key] != expected[key])
        raise BuildToolError(
            "resolved source manifest differs from its profile lock "
            f"(missing={missing[:3]}, extra={extra[:3]}, changed={changed[:3]})"
        )


def check_manifest_update(
    profile: Profile,
    output_dir: Path,
    *,
    runner: CommandRunner | None = None,
) -> bool:
    runner = runner or CommandRunner()
    result = runner.run(
        ["git", "ls-remote", profile.manifest_url, f"refs/heads/{profile.manifest_branch}"],
        capture=True,
    )
    fields = result.stdout.strip().split()
    if len(fields) != 2 or not is_full_commit(fields[0]):
        raise BuildToolError("could not resolve the official manifest branch")
    remote_commit = fields[0]
    changes: list[tuple[str, str, str]] = []
    if remote_commit != profile.manifest_revision:
        changes.append(("kernel_manifest", profile.manifest_revision, remote_commit))

    # The lockfiles annotate the moving OnePlus branch used to resolve the
    # three device-owned projects.  Compare those refs to the locked SHAs too;
    # this catches a force-push or source update even before the manifest repo
    # itself is refreshed.
    lock_text = profile.locked_manifest.read_text(encoding="utf-8")
    branch_match = re.search(r"upstream branch ([A-Za-z0-9_./-]+)\.", lock_text)
    if branch_match is None:
        raise BuildToolError(f"{profile.locked_manifest}: missing upstream branch annotation")
    upstream_ref = f"refs/heads/{branch_match.group(1)}"
    lock_root = ET.parse(profile.locked_manifest).getroot()
    remotes = {node.get("name"): node.get("fetch") for node in lock_root.findall("remote")}
    for project in lock_root.findall("project"):
        if project.get("remote") != "origin":
            continue
        name = project.get("name")
        locked_revision = project.get("revision")
        fetch = remotes.get("origin")
        if not name or not is_full_commit(locked_revision) or not fetch:
            raise BuildToolError("locked OnePlus project metadata is incomplete")
        project_url = f"{fetch.rstrip('/')}/{name}.git"
        remote_result = runner.run(["git", "ls-remote", project_url, upstream_ref], capture=True)
        remote_fields = remote_result.stdout.strip().split()
        if len(remote_fields) != 2 or not is_full_commit(remote_fields[0]):
            raise BuildToolError(f"could not resolve {project_url} {upstream_ref}")
        if remote_fields[0] != locked_revision:
            changes.append((name, locked_revision, remote_fields[0]))
    changed = bool(changes)
    output_dir.mkdir(parents=True, exist_ok=True)
    report = output_dir / "source-changes.md"
    status = "changed" if changed else "unchanged"
    report.write_text(
        "# OnePlus source monitor\n\n"
        f"- Profile: `{profile.id}`\n"
        f"- Branch: `{profile.manifest_branch}`\n"
        f"- Locked commit: `{profile.manifest_revision}`\n"
        f"- Remote commit: `{remote_commit}`\n"
        f"- Status: **{status}**\n",
        encoding="utf-8",
        newline="\n",
    )
    if changes:
        with report.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write("\n## Changed locks\n\n| Project | Locked | Remote |\n|---|---|---|\n")
            for name, locked_revision, latest in changes:
                handle.write(f"| `{name}` | `{locked_revision}` | `{latest}` |\n")
    return changed


def _smoke_manifest(profile: Profile, destination: Path) -> None:
    revision = "0" * 40
    destination.write_text(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<manifest>\n"
        "  <remote name=\"smoke\" fetch=\"https://example.invalid/\" />\n"
        f"  <project name=\"smoke/{profile.id}\" path=\"kernel_platform/common\" revision=\"{revision}\" />\n"
        "</manifest>\n",
        encoding="utf-8",
        newline="\n",
    )


def sync_sources(
    profile: Profile,
    lock: DependencyLock,
    output_dir: Path,
    cache_root: Path,
    *,
    jobs: int,
    dry_run: bool,
    smoke: bool,
) -> tuple[Path, Path]:
    if jobs < 1:
        raise BuildToolError("jobs must be positive")
    # The CLI contract treats --output as the exact source checkout directory.
    output_dir = output_dir.resolve()
    source_dir = output_dir
    metadata_dir = source_dir / ".op13"
    resolved_manifest = metadata_dir / f"{profile.id}-manifest-resolved.xml"
    context_path = metadata_dir / "build-context.json"
    if dry_run:
        print(f"would synchronize {profile.id} at {profile.manifest_revision} into {source_dir}")
        return source_dir, context_path
    source_dir.mkdir(parents=True, exist_ok=True)
    metadata_dir.mkdir(parents=True, exist_ok=True)
    validate_resolved_manifest(profile.locked_manifest)
    if smoke:
        (source_dir / "kernel_platform" / "common").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(profile.locked_manifest, resolved_manifest)
    else:
        fetch_dependencies(
            lock,
            cache_root,
            selected=("repo_launcher",),
            offline=False,
        )
        launcher = _repo_launcher_path(cache_root, lock)
        if not launcher.is_file() or sha256_file(launcher) != lock.dependencies["repo_launcher"].sha256:
            raise BuildToolError("verified repo launcher is absent")
        repo_url, repo_commit = _repo_implementation_pin(lock)
        runner = CommandRunner()
        runner.run(
            [
                sys.executable,
                str(launcher),
                "init",
                "-u",
                profile.manifest_url,
                "-b",
                profile.manifest_revision,
                "-m",
                profile.manifest_file,
                "--repo-url",
                repo_url,
                "--repo-rev",
                repo_commit,
                "--no-repo-verify",
                *REPO_INIT_STORAGE_FLAGS,
            ],
            cwd=source_dir,
        )
        local_manifest_name = f".op13-{profile.id}-locked.xml"
        local_manifest = source_dir / ".repo" / "manifests" / local_manifest_name
        shutil.copyfile(profile.locked_manifest, local_manifest)
        runner.run(
            [
                sys.executable,
                str(launcher),
                "init",
                "-m",
                local_manifest_name,
                "--repo-url",
                repo_url,
                "--repo-rev",
                repo_commit,
                "--no-repo-verify",
                *REPO_INIT_STORAGE_FLAGS,
            ],
            cwd=source_dir,
        )
        runner.run(
            [
                sys.executable,
                str(launcher),
                "sync",
                *REPO_SYNC_STORAGE_FLAGS,
                "--fail-fast",
                "-j",
                str(jobs),
            ],
            cwd=source_dir,
        )
        result = runner.run(
            [sys.executable, str(launcher), "manifest", "-r"],
            cwd=source_dir,
            capture=True,
        )
        resolved_manifest.write_text(result.stdout, encoding="utf-8", newline="\n")
        repo_head = runner.run(
            ["git", "rev-parse", "HEAD"],
            cwd=source_dir / ".repo" / "repo",
            capture=True,
        ).stdout.strip()
        if repo_head != repo_commit:
            raise BuildToolError("Android repo implementation does not match its lock pin")
    validate_resolved_manifest(resolved_manifest)
    assert_manifest_matches_lock(resolved_manifest, profile.locked_manifest)
    context = new_context(profile, lock, resolved_manifest, smoke=smoke)
    write_context(context_path, context)
    return source_dir, context_path


def monitor_or_raise(profile: Profile, output_dir: Path) -> None:
    if check_manifest_update(profile, output_dir):
        raise SourceChanged(
            f"official manifest branch moved beyond {profile.manifest_revision}; see {output_dir / 'source-changes.md'}"
        )
