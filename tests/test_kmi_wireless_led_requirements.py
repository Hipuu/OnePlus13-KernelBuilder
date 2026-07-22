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
SCRIPT = ROOT / "scripts" / "integrate-kmi-wireless-led-requirements.py"
SPEC = importlib.util.spec_from_file_location("integrate_kmi_wireless_led", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
HELPER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = HELPER
SPEC.loader.exec_module(HELPER)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class KmiWirelessLedRequirementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.common = Path(self.temporary.name) / "common"
        (self.common / "android").mkdir(parents=True)
        self.target = self.common / HELPER.TARGET_RELATIVE
        self.preimage = b"[abi_symbol_list]\n  from_kuid_munged\n"
        self.postimage = self.preimage + b"".join(
            f"  {entry['symbol']}\n".encode("ascii") for entry in HELPER.SYMBOLS
        )
        self.target.write_bytes(self.preimage)
        self.contracts = {
            "fixture": {
                "pre_size": len(self.preimage),
                "pre_sha256": _sha256(self.preimage),
                "post_size": len(self.postimage),
                "post_sha256": _sha256(self.postimage),
            }
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _integrate(self) -> dict[str, object]:
        with patch.object(HELPER, "CONTRACTS", self.contracts):
            return HELPER.integrate(self.common, "fixture")

    def test_adds_only_two_required_exports_and_is_idempotent(self) -> None:
        first = self._integrate()
        self.assertEqual(self.target.read_bytes(), self.postimage)
        self.assertEqual(first["strict_mode"], True)
        self.assertEqual(
            [record["symbol"] for record in first["symbols"]],
            [
                "__ieee80211_get_radio_led_name",
                "__ieee80211_create_tpt_led_trigger",
            ],
        )
        self.assertTrue(all(record["status"] == "integrated" for record in first["symbols"]))

        second = self._integrate()
        self.assertTrue(
            all(record["status"] == "already-integrated" for record in second["symbols"])
        )
        stamp = json.loads((self.common / HELPER.STAMP_NAME).read_text(encoding="utf-8"))
        self.assertEqual(stamp, second)

    def test_partial_or_unknown_preimage_fails_closed(self) -> None:
        payloads = (
            self.preimage + f"  {HELPER.SYMBOLS[0]['symbol']}\n".encode("ascii"),
            self.preimage + b"  unexpected\n",
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                self.target.write_bytes(payload)
                with self.assertRaisesRegex(HELPER.IntegrationError, "pre/postimage changed"):
                    self._integrate()

    def test_stamp_write_failure_rolls_back_source(self) -> None:
        original_atomic_write = HELPER._atomic_write
        calls = 0

        def fail_stamp(path: Path, payload: bytes, *, mode: int) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("synthetic stamp failure")
            original_atomic_write(path, payload, mode=mode)

        with (
            patch.object(HELPER, "CONTRACTS", self.contracts),
            patch.object(HELPER, "_atomic_write", side_effect=fail_stamp),
            self.assertRaisesRegex(HELPER.IntegrationError, "synthetic stamp failure"),
        ):
            HELPER.integrate(self.common, "fixture")
        self.assertEqual(self.target.read_bytes(), self.preimage)
        self.assertFalse((self.common / HELPER.STAMP_NAME).exists())

    def test_crlf_and_nonterminated_lists_fail_closed(self) -> None:
        for payload in (self.preimage.replace(b"\n", b"\r\n"), self.preimage.rstrip(b"\n")):
            with self.subTest(payload=payload):
                self.target.write_bytes(payload)
                with self.assertRaisesRegex(HELPER.IntegrationError, "LF-terminated"):
                    self._integrate()

    def test_symlinked_target_and_common_tree_are_rejected(self) -> None:
        outside = Path(self.temporary.name) / "outside"
        outside.write_bytes(self.preimage)
        self.target.unlink()
        try:
            self.target.symlink_to(outside)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"symlinks are unavailable: {exc}")
        with self.assertRaisesRegex(HELPER.IntegrationError, "missing or unsafe"):
            self._integrate()

        self.target.unlink()
        self.target.write_bytes(self.preimage)
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

    def test_checked_contracts_and_consumers_are_exact(self) -> None:
        self.assertEqual(set(HELPER.CONTRACTS), {"oos15-cn", "oos15-global", "oos16"})
        for base, contract in HELPER.CONTRACTS.items():
            with self.subTest(base=base):
                HELPER._validate_internal_contract(base, contract)
        self.assertEqual(
            HELPER.SYMBOLS,
            (
                {
                    "symbol": "__ieee80211_get_radio_led_name",
                    "consumers": ["ath9k.ko", "ath9k_htc.ko"],
                },
                {
                    "symbol": "__ieee80211_create_tpt_led_trigger",
                    "consumers": ["ath9k.ko", "ath9k_htc.ko", "mt76.ko"],
                },
            ),
        )

    def test_series_gate_is_feature_scoped_and_ordered(self) -> None:
        series = json.loads(
            (ROOT / "patches" / "series" / "common.yml").read_text(encoding="utf-8")
        )
        operations = series["operations"]
        ids = [operation["id"] for operation in operations]
        core_index = ids.index("kmi-symbol-list-vendor-module-closure")
        wireless_index = ids.index("kmi-symbol-list-wireless-led-closure")
        self.assertEqual(wireless_index, core_index + 1)
        operation = operations[wireless_index]
        self.assertEqual(operation["feature"], "nethunter.wifi_ath")
        self.assertEqual(operation["type"], "exec")
        self.assertIn(
            "{repo_root}/scripts/integrate-kmi-wireless-led-requirements.py",
            operation["argv"],
        )
        self.assertEqual(
            operation["expected_outputs"],
            ["kernel_platform/common/.op13-kmi-wireless-led-exports.json"],
        )


if __name__ == "__main__":
    unittest.main()
