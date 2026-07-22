#!/usr/bin/env python3
"""Verify, extract, and execute the repository's pinned ShellCheck release."""

from __future__ import annotations

import argparse
import hashlib
import os
import stat
import subprocess
import tarfile
import tempfile
from pathlib import Path


ARCHIVE_SIZE = 2_559_196
ARCHIVE_SHA256 = "8c3be12b05d5c177a04c29e3c78ce89ac86f1595681cab149b65b97c4e227198"
ARCHIVE_MEMBERS = (
    "shellcheck-v0.11.0/LICENSE.txt",
    "shellcheck-v0.11.0/README.txt",
    "shellcheck-v0.11.0/shellcheck",
)
MEMBER_CONTRACTS = {
    "shellcheck-v0.11.0/LICENSE.txt": (
        0o644,
        35_149,
        "3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986",
    ),
    "shellcheck-v0.11.0/README.txt": (
        0o644,
        2_374,
        "2e1ea8f9108e2aff34c76039f2ceae989e1780ca4c85caefe3e722a98b766235",
    ),
    "shellcheck-v0.11.0/shellcheck": (
        0o755,
        16_213_136,
        "4da528ddb3a4d1b7b24a59d4e16eb2f5fd960f4bd9a3708a15baddbdf1d5a55b",
    ),
}
BINARY_MEMBER = "shellcheck-v0.11.0/shellcheck"


class ShellCheckError(RuntimeError):
    """The pinned ShellCheck artifact does not match its exact contract."""


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def verified_binary(archive_path: Path) -> bytes:
    if archive_path.is_symlink() or not archive_path.is_file():
        raise ShellCheckError("ShellCheck archive must be a plain regular file")
    payload = archive_path.read_bytes()
    if len(payload) != ARCHIVE_SIZE or _sha256(payload) != ARCHIVE_SHA256:
        raise ShellCheckError("ShellCheck archive differs from the pinned release asset")
    try:
        with tarfile.open(archive_path, "r:xz") as archive:
            members = archive.getmembers()
            if tuple(member.name for member in members) != ARCHIVE_MEMBERS:
                raise ShellCheckError("ShellCheck archive member set or order differs")
            extracted: dict[str, bytes] = {}
            for member in members:
                contract = MEMBER_CONTRACTS.get(member.name)
                if contract is None or not member.isfile():
                    raise ShellCheckError("ShellCheck archive contains an unknown member")
                expected_mode, expected_size, expected_digest = contract
                if (
                    stat.S_IMODE(member.mode) != expected_mode
                    or member.size != expected_size
                ):
                    raise ShellCheckError(
                        f"ShellCheck member metadata differs: {member.name}"
                    )
                stream = archive.extractfile(member)
                if stream is None:
                    raise ShellCheckError(
                        f"ShellCheck member payload is absent: {member.name}"
                    )
                member_payload = stream.read(expected_size + 1)
                if (
                    len(member_payload) != expected_size
                    or _sha256(member_payload) != expected_digest
                ):
                    raise ShellCheckError(
                        f"ShellCheck member bytes differ: {member.name}"
                    )
                extracted[member.name] = member_payload
    except (OSError, tarfile.TarError) as exc:
        raise ShellCheckError("ShellCheck release asset is not a readable xz tar") from exc
    return extracted[BINARY_MEMBER]


def run_shellcheck(archive_path: Path, arguments: list[str]) -> int:
    if not arguments:
        raise ShellCheckError("ShellCheck arguments are required")
    binary = verified_binary(archive_path)
    with tempfile.TemporaryDirectory(prefix="op13-shellcheck-") as temporary_name:
        executable = Path(temporary_name) / "shellcheck"
        descriptor = os.open(executable, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(binary)
            stream.flush()
            os.fsync(stream.fileno())
        executable.chmod(0o700)
        return subprocess.run(
            [str(executable), *arguments],
            check=False,
        ).returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("shellcheck_arguments", nargs=argparse.REMAINDER)
    arguments = parser.parse_args()
    shellcheck_arguments = list(arguments.shellcheck_arguments)
    if shellcheck_arguments[:1] == ["--"]:
        shellcheck_arguments = shellcheck_arguments[1:]
    try:
        return run_shellcheck(arguments.archive, shellcheck_arguments)
    except ShellCheckError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
