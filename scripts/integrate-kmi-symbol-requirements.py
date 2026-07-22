#!/usr/bin/env python3
"""Add the minimal vendor-module exports to locked OnePlus GKI symbol lists."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping


STAMP_NAME = ".op13-kmi-symbol-exports.json"
SYMBOL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Each file is an immutable OnePlus common-tree preimage.  The postimage is the
# preimage plus exactly one indented symbol line and a trailing LF.
CONTRACTS: Mapping[str, tuple[Mapping[str, Any], ...]] = {
    "oos15-cn": (
        {
            "path": "android/abi_gki_aarch64_oplus",
            "symbol": "from_kuid",
            "consumer": "oplus_bsp_mm_osvelte.ko",
            "pre_size": 27193,
            "pre_sha256": "4fa5b93857be8126ce2e499f31535dc510d3713292f933f3b7e8d55af99c9bbd",
            "post_size": 27205,
            "post_sha256": "4ff0743a287ac2a6dc483a3814f6cc5cc982693e7cea00fc26533e5ee4f9c4e0",
        },
        {
            "path": "android/abi_gki_aarch64_qcom",
            "symbol": "from_kuid_munged",
            "consumer": "msm_sysstats.ko",
            "pre_size": 57666,
            "pre_sha256": "93a5ff7f40b614a426f6eb123780ac13f5a429b4329b28184fcf63b552e539ea",
            "post_size": 57685,
            "post_sha256": "7d2186f6e1a81bd50889a97b22a96b1655e6b62b86f4af76cb153a43c61d740e",
        },
    ),
    "oos15-global": (
        {
            "path": "android/abi_gki_aarch64_oplus",
            "symbol": "from_kuid",
            "consumer": "oplus_bsp_mm_osvelte.ko",
            "pre_size": 29273,
            "pre_sha256": "e0a3899989bcdaeafdd692b1bbe6cc3b47cd3c5deab0af8abcc1870c5edafaeb",
            "post_size": 29285,
            "post_sha256": "637072df10e65509adc5ca4f246febb8d3d3bc61ec047efa324c725077e833ea",
        },
        {
            "path": "android/abi_gki_aarch64_qcom",
            "symbol": "from_kuid_munged",
            "consumer": "msm_sysstats.ko",
            "pre_size": 57699,
            "pre_sha256": "6ed98aacda374fcdb838d62d2544577d9a988e73839b6482efd851862db2a729",
            "post_size": 57718,
            "post_sha256": "5547fcc2e4bdb4ff96762d66f12f258cab754d8f207415ede9af76a93d3011af",
        },
    ),
    "oos16": (
        {
            "path": "android/abi_gki_aarch64_oplus",
            "symbol": "from_kuid",
            "consumer": "oplus_bsp_mm_osvelte.ko",
            "pre_size": 20312,
            "pre_sha256": "322260cf30721f9f42f33ed26b0d58e88318e2a461cb8d94367641085d842199",
            "post_size": 20324,
            "post_sha256": "b3a203686c642e64ea976e10a927a5d46aa7053d32aec78e2d4c27b49ff9137c",
        },
        {
            "path": "android/abi_gki_aarch64_qcom",
            "symbol": "from_kuid_munged",
            "consumer": "msm_sysstats.ko",
            "pre_size": 57699,
            "pre_sha256": "6ed98aacda374fcdb838d62d2544577d9a988e73839b6482efd851862db2a729",
            "post_size": 57718,
            "post_sha256": "5547fcc2e4bdb4ff96762d66f12f258cab754d8f207415ede9af76a93d3011af",
        },
    ),
}


class IntegrationError(RuntimeError):
    """A locked source precondition or postcondition was not satisfied."""


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _plain_file(root: Path, relative: str) -> Path:
    path = root.joinpath(*Path(relative).parts)
    if path.is_symlink() or not path.is_file():
        raise IntegrationError(
            "locked GKI symbol list is missing or not a regular file: "
            f"{relative}"
        )
    try:
        canonical = path.resolve(strict=True)
    except OSError as exc:
        raise IntegrationError(f"cannot resolve locked GKI symbol list {relative}: {exc}") from exc
    if not _inside(canonical, root):
        raise IntegrationError(f"locked GKI symbol list escapes the common tree: {relative}")
    return path


def _atomic_write(path: Path, payload: bytes, *, mode: int) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _validate_contract(contract: Mapping[str, Any]) -> None:
    required = {
        "path",
        "symbol",
        "consumer",
        "pre_size",
        "pre_sha256",
        "post_size",
        "post_sha256",
    }
    if set(contract) != required:
        raise IntegrationError("internal KMI symbol contract fields differ")
    if (
        not isinstance(contract["path"], str)
        or not contract["path"]
        or Path(contract["path"]).is_absolute()
        or ".." in Path(contract["path"]).parts
        or not isinstance(contract["symbol"], str)
        or SYMBOL_RE.fullmatch(contract["symbol"]) is None
        or not isinstance(contract["consumer"], str)
        or not contract["consumer"].endswith(".ko")
    ):
        raise IntegrationError("internal KMI symbol contract identity is invalid")
    for field in ("pre_size", "post_size"):
        if (
            not isinstance(contract[field], int)
            or isinstance(contract[field], bool)
            or contract[field] < 1
        ):
            raise IntegrationError(f"internal KMI symbol contract {field} is invalid")
    for field in ("pre_sha256", "post_sha256"):
        if (
            not isinstance(contract[field], str)
            or re.fullmatch(r"[0-9a-f]{64}", contract[field]) is None
        ):
            raise IntegrationError(f"internal KMI symbol contract {field} is invalid")


def _prepare_one(
    common: Path,
    contract: Mapping[str, Any],
) -> tuple[dict[str, Any], Path, bytes, bytes, int, bool]:
    _validate_contract(contract)
    relative = str(contract["path"])
    symbol = str(contract["symbol"])
    symbol_line = f"  {symbol}\n".encode("ascii")
    path = _plain_file(common, relative)
    original = path.read_bytes()
    if b"\r" in original or not original.endswith(b"\n"):
        raise IntegrationError(f"{relative}: expected an LF-terminated symbol list")
    original_size = len(original)
    original_sha256 = _sha256(original)

    if (
        original_size == contract["post_size"]
        and original_sha256 == contract["post_sha256"]
    ):
        updated = original
        status = "already-integrated"
        needs_write = False
    elif (
        original_size == contract["pre_size"]
        and original_sha256 == contract["pre_sha256"]
    ):
        if symbol_line in original.splitlines(keepends=True):
            raise IntegrationError(
                f"{relative}: {symbol} is unexpectedly present in the preimage"
            )
        updated = original + symbol_line
        status = "integrated"
        needs_write = True
    else:
        raise IntegrationError(
            f"{relative}: locked pre/postimage changed; "
            f"got {original_size} bytes/{original_sha256}"
        )

    if (
        len(updated) != contract["post_size"]
        or _sha256(updated) != contract["post_sha256"]
        or updated.splitlines().count(symbol_line.rstrip(b"\n")) != 1
    ):
        raise IntegrationError(f"{relative}: {symbol} postimage verification failed")
    record = {
        "path": relative,
        "symbol": symbol,
        "consumer": contract["consumer"],
        "status": status,
        "pre_size": contract["pre_size"],
        "pre_sha256": contract["pre_sha256"],
        "post_size": contract["post_size"],
        "post_sha256": contract["post_sha256"],
    }
    return (
        record,
        path,
        original,
        updated,
        path.stat().st_mode & 0o777,
        needs_write,
    )


def integrate(common_dir: Path, base: str) -> dict[str, Any]:
    if base not in CONTRACTS:
        raise IntegrationError(f"unsupported OnePlus base {base!r}")
    if common_dir.is_symlink():
        raise IntegrationError(
            f"locked common tree must not be a symlink: {common_dir}"
        )
    try:
        common = common_dir.resolve(strict=True)
    except OSError as exc:
        raise IntegrationError(f"locked common tree is missing: {common_dir}") from exc
    if not common.is_dir():
        raise IntegrationError(f"locked common tree is not a regular directory: {common}")

    # Validate every locked pre/postimage before mutating either symbol list.
    # A stale contract must never leave a half-applied source tree.
    prepared = [_prepare_one(common, contract) for contract in CONTRACTS[base]]
    written: list[tuple[Path, bytes, int]] = []

    def rollback() -> list[str]:
        errors: list[str] = []
        for path, original, mode in reversed(written):
            try:
                _atomic_write(path, original, mode=mode)
            except OSError as exc:
                errors.append(f"{path}: {exc}")
        return errors

    try:
        for _, path, original, updated, mode, needs_write in prepared:
            if not needs_write:
                continue
            if path.read_bytes() != original:
                raise IntegrationError(
                    f"{path}: KMI symbol list changed after contract preflight"
                )
            _atomic_write(path, updated, mode=mode)
            written.append((path, original, mode))
        for _, path, _, updated, _, _ in prepared:
            if path.read_bytes() != updated:
                raise IntegrationError(
                    f"{path}: committed KMI postimage verification failed"
                )

        records = [record for record, *_ in prepared]
        document = {
            "schema_version": 1,
            "integration": "minimal-vendor-module-kmi-symbol-closure",
            "base": base,
            "strict_mode": True,
            "symbols": records,
        }
        stamp = common / STAMP_NAME
        payload = (
            json.dumps(document, indent=2, sort_keys=True, separators=(",", ": "))
            + "\n"
        ).encode("utf-8")
        _atomic_write(stamp, payload, mode=0o644)
    except (OSError, IntegrationError) as exc:
        rollback_errors = rollback()
        detail = (
            "; rollback failures: " + "; ".join(rollback_errors)
            if rollback_errors
            else ""
        )
        raise IntegrationError(
            f"cannot atomically update KMI symbol lists: {exc}{detail}"
        ) from exc
    return document


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--common-dir", type=Path, required=True)
    parser.add_argument("--base", choices=sorted(CONTRACTS), required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        document = integrate(args.common_dir, args.base)
    except IntegrationError as exc:
        print(f"KMI symbol integration: {exc}", file=os.sys.stderr)
        return 1
    print(json.dumps(document, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
