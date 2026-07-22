#!/usr/bin/env python3
"""Verify, extract, and execute the repository's pinned actionlint release."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


ARCHIVE_SIZE = 2_265_519
ARCHIVE_SHA256 = "900919a84f2229bac68ca9cd4103ea297abc35e9689ebb842c6e34a3d1b01b0a"
BINARY_SIZE = 5_779_640
BINARY_SHA256 = "1ef54b3443db3e2c2ef3b82c565c328ce1d76420ae13e0df3676f936dfcdb77c"
ARCHIVE_MEMBERS = (
    "LICENSE.txt",
    "README.md",
    "docs/README.md",
    "docs/api.md",
    "docs/checks.md",
    "docs/config.md",
    "docs/install.md",
    "docs/reference.md",
    "docs/usage.md",
    "man/actionlint.1",
    "actionlint",
)


class ActionlintError(RuntimeError):
    """The pinned actionlint artifact does not match its exact contract."""


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def verified_binary(archive_path: Path) -> bytes:
    if archive_path.is_symlink() or not archive_path.is_file():
        raise ActionlintError("actionlint archive must be a plain regular file")
    payload = archive_path.read_bytes()
    if len(payload) != ARCHIVE_SIZE or _sha256(payload) != ARCHIVE_SHA256:
        raise ActionlintError("actionlint archive differs from the pinned release asset")
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            members = archive.getmembers()
            if tuple(member.name for member in members) != ARCHIVE_MEMBERS:
                raise ActionlintError("actionlint archive member set or order differs")
            if any(not member.isfile() for member in members):
                raise ActionlintError("actionlint archive contains a non-regular member")
            binary_member = members[-1]
            if (
                binary_member.name != "actionlint"
                or stat.S_IMODE(binary_member.mode) != 0o755
                or binary_member.size != BINARY_SIZE
            ):
                raise ActionlintError("actionlint binary metadata differs")
            stream = archive.extractfile(binary_member)
            if stream is None:
                raise ActionlintError("actionlint binary payload is absent")
            binary = stream.read(BINARY_SIZE + 1)
    except (OSError, tarfile.TarError) as exc:
        raise ActionlintError("actionlint release asset is not a readable gzip tar") from exc
    if len(binary) != BINARY_SIZE or _sha256(binary) != BINARY_SHA256:
        raise ActionlintError("actionlint binary differs from the pinned release")
    return binary


def verified_shellcheck_binary(archive_path: Path) -> bytes:
    runner_path = Path(__file__).with_name("run-pinned-shellcheck.py")
    spec = importlib.util.spec_from_file_location(
        "op13_run_pinned_shellcheck",
        runner_path,
    )
    if spec is None or spec.loader is None:
        raise ActionlintError("pinned ShellCheck verifier could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        return module.verified_binary(archive_path)
    except Exception as exc:
        if exc.__class__.__name__ == "ShellCheckError":
            raise ActionlintError(str(exc)) from exc
        raise
    finally:
        sys.modules.pop(spec.name, None)


def _write_executable(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    path.chmod(0o700)


def run_actionlint(
    archive_path: Path,
    shellcheck_archive_path: Path,
    paths: list[str],
) -> int:
    binary = verified_binary(archive_path)
    shellcheck_binary = verified_shellcheck_binary(shellcheck_archive_path)
    with tempfile.TemporaryDirectory(prefix="op13-actionlint-") as temporary_name:
        executable = Path(temporary_name) / "actionlint"
        shellcheck = Path(temporary_name) / "shellcheck"
        _write_executable(executable, binary)
        _write_executable(shellcheck, shellcheck_binary)
        command = [
            str(executable),
            "-no-color",
            f"-shellcheck={shellcheck}",
            "-pyflakes=",
            *paths,
        ]
        return subprocess.run(command, check=False).returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--shellcheck-archive", required=True, type=Path)
    parser.add_argument("paths", nargs="*")
    arguments = parser.parse_args()
    try:
        return run_actionlint(
            arguments.archive,
            arguments.shellcheck_archive,
            arguments.paths,
        )
    except ActionlintError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
