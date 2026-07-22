#!/usr/bin/env python3
"""Retain two mac80211 LED exports required by selected NetHunter modules."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping


STAMP_NAME = ".op13-kmi-wireless-led-exports.json"
TARGET_RELATIVE = Path("android/abi_gki_aarch64_qcom")
SYMBOLS: tuple[Mapping[str, Any], ...] = (
    {
        "symbol": "__ieee80211_get_radio_led_name",
        "consumers": ["ath9k.ko", "ath9k_htc.ko"],
    },
    {
        "symbol": "__ieee80211_create_tpt_led_trigger",
        "consumers": ["ath9k.ko", "ath9k_htc.ko", "mt76.ko"],
    },
)

# The preimage is the exact qcom list after the mandatory core KMI closure has
# appended from_kuid_munged. The postimage appends only the two exports above.
CONTRACTS: Mapping[str, Mapping[str, Any]] = {
    "oos15-cn": {
        "pre_size": 57685,
        "pre_sha256": "7d2186f6e1a81bd50889a97b22a96b1655e6b62b86f4af76cb153a43c61d740e",
        "post_size": 57755,
        "post_sha256": "fa816bc444f8b923c3577f6e13fdf17234d8cede913f46b380bf949272e1f38d",
    },
    "oos15-global": {
        "pre_size": 57718,
        "pre_sha256": "5547fcc2e4bdb4ff96762d66f12f258cab754d8f207415ede9af76a93d3011af",
        "post_size": 57788,
        "post_sha256": "c571d96cf02c5ac4f4c5478c33b8d49efe8f11693df787693b0caad2df7730a6",
    },
    "oos16": {
        "pre_size": 57718,
        "pre_sha256": "5547fcc2e4bdb4ff96762d66f12f258cab754d8f207415ede9af76a93d3011af",
        "post_size": 57788,
        "post_sha256": "c571d96cf02c5ac4f4c5478c33b8d49efe8f11693df787693b0caad2df7730a6",
    },
}


class IntegrationError(RuntimeError):
    """A locked wireless KMI source contract was not satisfied."""


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _atomic_write(path: Path, payload: bytes, *, mode: int) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
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
        temporary.unlink(missing_ok=True)


def _validate_internal_contract(base: str, contract: Mapping[str, Any]) -> None:
    if set(contract) != {"pre_size", "pre_sha256", "post_size", "post_sha256"}:
        raise IntegrationError(f"internal wireless KMI contract fields differ for {base}")
    for field in ("pre_size", "post_size"):
        value = contract[field]
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise IntegrationError(f"internal wireless KMI {field} is invalid for {base}")
    for field in ("pre_sha256", "post_sha256"):
        value = contract[field]
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise IntegrationError(f"internal wireless KMI {field} is invalid for {base}")
    if contract["post_size"] - contract["pre_size"] != sum(
        len(f"  {entry['symbol']}\n".encode("ascii")) for entry in SYMBOLS
    ):
        raise IntegrationError(f"internal wireless KMI size delta is invalid for {base}")
    observed: set[str] = set()
    for entry in SYMBOLS:
        if set(entry) != {"symbol", "consumers"}:
            raise IntegrationError("internal wireless KMI symbol fields differ")
        symbol = entry["symbol"]
        consumers = entry["consumers"]
        if (
            not isinstance(symbol, str)
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", symbol) is None
            or symbol in observed
            or not isinstance(consumers, list)
            or not consumers
            or any(
                not isinstance(consumer, str)
                or re.fullmatch(r"[A-Za-z0-9_.+-]+\.ko", consumer) is None
                for consumer in consumers
            )
            or len(consumers) != len(set(consumers))
        ):
            raise IntegrationError("internal wireless KMI symbol identity is invalid")
        observed.add(symbol)


def _plain_common_tree(common_dir: Path) -> Path:
    raw = Path(common_dir)
    if raw.is_symlink():
        raise IntegrationError(f"locked common tree must not be a symlink: {raw}")
    try:
        common = raw.resolve(strict=True)
    except OSError as exc:
        raise IntegrationError(f"locked common tree is missing: {raw}") from exc
    if not common.is_dir():
        raise IntegrationError(f"locked common tree is not a directory: {common}")
    return common


def _plain_target(common: Path) -> Path:
    target = common / TARGET_RELATIVE
    if target.is_symlink() or not target.is_file():
        raise IntegrationError(
            f"locked qcom symbol list is missing or unsafe: {TARGET_RELATIVE.as_posix()}"
        )
    try:
        target.resolve(strict=True).relative_to(common)
    except (OSError, ValueError) as exc:
        raise IntegrationError("locked qcom symbol list escapes the common tree") from exc
    return target


def integrate(common_dir: Path, base: str) -> dict[str, Any]:
    contract = CONTRACTS.get(base)
    if contract is None:
        raise IntegrationError(f"unsupported OnePlus base {base!r}")
    _validate_internal_contract(base, contract)
    common = _plain_common_tree(common_dir)
    target = _plain_target(common)
    original = target.read_bytes()
    if b"\r" in original or not original.endswith(b"\n"):
        raise IntegrationError("qcom symbol list must be LF-terminated")
    identity = (len(original), _sha256(original))
    pre_identity = (contract["pre_size"], contract["pre_sha256"])
    post_identity = (contract["post_size"], contract["post_sha256"])
    if identity == post_identity:
        updated = original
        status = "already-integrated"
    elif identity == pre_identity:
        for entry in SYMBOLS:
            line = f"  {entry['symbol']}\n".encode("ascii")
            if line in original.splitlines(keepends=True):
                raise IntegrationError(
                    f"{entry['symbol']} is unexpectedly present in the wireless preimage"
                )
        updated = original + b"".join(
            f"  {entry['symbol']}\n".encode("ascii") for entry in SYMBOLS
        )
        status = "integrated"
    else:
        raise IntegrationError(
            "locked qcom wireless KMI pre/postimage changed; "
            f"got {identity[0]} bytes/{identity[1]}"
        )
    if (len(updated), _sha256(updated)) != post_identity:
        raise IntegrationError("wireless KMI postimage verification failed")
    lines = updated.splitlines()
    for entry in SYMBOLS:
        if lines.count(f"  {entry['symbol']}".encode("ascii")) != 1:
            raise IntegrationError(
                f"wireless KMI symbol multiplicity changed: {entry['symbol']}"
            )

    records = [
        {
            "path": TARGET_RELATIVE.as_posix(),
            "symbol": entry["symbol"],
            "consumers": list(entry["consumers"]),
            "status": status,
        }
        for entry in SYMBOLS
    ]
    document: dict[str, Any] = {
        "schema_version": 1,
        "integration": "nethunter-mac80211-led-kmi-symbol-closure",
        "feature": "nethunter.wifi_ath",
        "base": base,
        "strict_mode": True,
        "pre_size": contract["pre_size"],
        "pre_sha256": contract["pre_sha256"],
        "post_size": contract["post_size"],
        "post_sha256": contract["post_sha256"],
        "symbols": records,
    }
    stamp_payload = (
        json.dumps(document, indent=2, sort_keys=True, separators=(",", ": ")) + "\n"
    ).encode("utf-8")
    mode = target.stat().st_mode & 0o777
    changed = updated != original
    try:
        if changed:
            if target.read_bytes() != original:
                raise IntegrationError("qcom symbol list changed after contract preflight")
            _atomic_write(target, updated, mode=mode)
        if target.read_bytes() != updated:
            raise IntegrationError("committed wireless KMI postimage changed")
        _atomic_write(common / STAMP_NAME, stamp_payload, mode=0o644)
    except (OSError, IntegrationError) as exc:
        if changed:
            try:
                _atomic_write(target, original, mode=mode)
            except OSError as rollback_exc:
                raise IntegrationError(
                    f"wireless KMI integration failed: {exc}; rollback failed: {rollback_exc}"
                ) from exc
        raise IntegrationError(f"wireless KMI integration failed: {exc}") from exc
    return document


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--common-dir", type=Path, required=True)
    parser.add_argument("--base", choices=sorted(CONTRACTS), required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        document = integrate(args.common_dir, args.base)
    except IntegrationError as exc:
        print(f"wireless KMI integration: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(document, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
