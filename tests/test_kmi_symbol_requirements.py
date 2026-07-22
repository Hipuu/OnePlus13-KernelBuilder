from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "integrate-kmi-symbol-requirements.py"
SPEC = importlib.util.spec_from_file_location("integrate_kmi_symbol_requirements", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
HELPER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = HELPER
SPEC.loader.exec_module(HELPER)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class KmiSymbolRequirementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.common = Path(self.temporary.name) / "common"
        (self.common / "android").mkdir(parents=True)
        self.oplus = self.common / "android" / "abi_gki_aarch64_oplus"
        self.qcom = self.common / "android" / "abi_gki_aarch64_qcom"
        self.oplus_pre = b"[abi_symbol_list]\n  existing_oplus\n"
        self.qcom_pre = b"[abi_symbol_list]\n  existing_qcom\n"
        self.oplus.write_bytes(self.oplus_pre)
        self.qcom.write_bytes(self.qcom_pre)
        self.contracts = {
            "fixture": (
                self._contract(
                    "android/abi_gki_aarch64_oplus",
                    "from_kuid",
                    "oplus_bsp_mm_osvelte.ko",
                    self.oplus_pre,
                ),
                self._contract(
                    "android/abi_gki_aarch64_qcom",
                    "from_kuid_munged",
                    "msm_sysstats.ko",
                    self.qcom_pre,
                ),
            )
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _contract(
        path: str,
        symbol: str,
        consumer: str,
        preimage: bytes,
    ) -> dict[str, object]:
        postimage = preimage + f"  {symbol}\n".encode("ascii")
        return {
            "path": path,
            "symbol": symbol,
            "consumer": consumer,
            "pre_size": len(preimage),
            "pre_sha256": _sha256(preimage),
            "post_size": len(postimage),
            "post_sha256": _sha256(postimage),
        }

    def _integrate(self) -> dict[str, object]:
        with patch.object(HELPER, "CONTRACTS", self.contracts):
            return HELPER.integrate(self.common, "fixture")

    def test_adds_only_the_two_required_symbols_and_is_idempotent(self) -> None:
        first = self._integrate()
        self.assertEqual(first["strict_mode"], True)
        self.assertEqual(
            self.oplus.read_text(encoding="utf-8").splitlines()[-1],
            "  from_kuid",
        )
        self.assertEqual(
            self.qcom.read_text(encoding="utf-8").splitlines()[-1],
            "  from_kuid_munged",
        )
        self.assertEqual(
            [record["status"] for record in first["symbols"]],
            ["integrated", "integrated"],
        )

        second = self._integrate()
        self.assertEqual(
            [record["status"] for record in second["symbols"]],
            ["already-integrated", "already-integrated"],
        )
        stamp = json.loads(
            (self.common / HELPER.STAMP_NAME).read_text(encoding="utf-8")
        )
        self.assertEqual(stamp, second)

    def test_unknown_preimage_fails_closed(self) -> None:
        self.oplus.write_bytes(self.oplus_pre + b"  unexpected\n")
        with self.assertRaisesRegex(HELPER.IntegrationError, "pre/postimage changed"):
            self._integrate()

    def test_second_unknown_preimage_does_not_partially_update_first(self) -> None:
        self.qcom.write_bytes(self.qcom_pre + b"  unexpected\n")
        with self.assertRaisesRegex(HELPER.IntegrationError, "pre/postimage changed"):
            self._integrate()
        self.assertEqual(self.oplus.read_bytes(), self.oplus_pre)
        self.assertFalse((self.common / HELPER.STAMP_NAME).exists())

    def test_second_write_failure_rolls_back_first(self) -> None:
        original_atomic_write = HELPER._atomic_write
        calls = 0

        def fail_second(path: Path, payload: bytes, *, mode: int) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("synthetic second-write failure")
            original_atomic_write(path, payload, mode=mode)

        with (
            patch.object(HELPER, "CONTRACTS", self.contracts),
            patch.object(HELPER, "_atomic_write", side_effect=fail_second),
            self.assertRaisesRegex(
                HELPER.IntegrationError,
                "synthetic second-write failure",
            ),
        ):
            HELPER.integrate(self.common, "fixture")
        self.assertEqual(self.oplus.read_bytes(), self.oplus_pre)
        self.assertEqual(self.qcom.read_bytes(), self.qcom_pre)
        self.assertFalse((self.common / HELPER.STAMP_NAME).exists())

    def test_crlf_or_nonterminated_symbol_list_fails_closed(self) -> None:
        for payload in (
            self.oplus_pre.replace(b"\n", b"\r\n"),
            self.oplus_pre.rstrip(b"\n"),
        ):
            with self.subTest(payload=payload):
                self.oplus.write_bytes(payload)
                with self.assertRaisesRegex(
                    HELPER.IntegrationError,
                    "LF-terminated",
                ):
                    self._integrate()
                self.oplus.write_bytes(self.oplus_pre)

    def test_symlinked_symbol_list_is_rejected(self) -> None:
        target = Path(self.temporary.name) / "outside"
        target.write_bytes(self.oplus_pre)
        self.oplus.unlink()
        try:
            self.oplus.symlink_to(target)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"file symlinks are unavailable: {exc}")
        with self.assertRaisesRegex(
            HELPER.IntegrationError,
            "missing or not a regular file",
        ):
            self._integrate()

    def test_symlinked_common_directory_is_rejected(self) -> None:
        link = Path(self.temporary.name) / "common-link"
        try:
            link.symlink_to(self.common, target_is_directory=True)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"directory symlinks are unavailable: {exc}")
        with (
            patch.object(HELPER, "CONTRACTS", self.contracts),
            self.assertRaisesRegex(HELPER.IntegrationError, "must not be a symlink"),
        ):
            HELPER.integrate(link, "fixture")

    def test_checked_in_contracts_match_all_locked_profiles(self) -> None:
        self.assertEqual(
            set(HELPER.CONTRACTS),
            {"oos15-cn", "oos15-global", "oos16"},
        )
        for base, contracts in HELPER.CONTRACTS.items():
            with self.subTest(base=base):
                self.assertEqual(
                    [(item["symbol"], item["consumer"]) for item in contracts],
                    [
                        ("from_kuid", "oplus_bsp_mm_osvelte.ko"),
                        ("from_kuid_munged", "msm_sysstats.ko"),
                    ],
                )
                for contract in contracts:
                    HELPER._validate_contract(contract)

    def test_common_series_runs_the_gate_before_feature_patches(self) -> None:
        series = json.loads(
            (ROOT / "patches" / "series" / "common.yml").read_text(
                encoding="utf-8"
            )
        )
        operations = series["operations"]
        ids = [operation["id"] for operation in operations]
        gate_index = ids.index("kmi-symbol-list-vendor-module-closure")
        self.assertLess(gate_index, ids.index("kbuild-o3-choice"))
        gate = operations[gate_index]
        self.assertEqual(gate["type"], "exec")
        self.assertIn(
            "{repo_root}/scripts/integrate-kmi-symbol-requirements.py",
            gate["argv"],
        )
        self.assertEqual(
            gate["expected_outputs"],
            ["kernel_platform/common/.op13-kmi-symbol-exports.json"],
        )


if __name__ == "__main__":
    unittest.main()
