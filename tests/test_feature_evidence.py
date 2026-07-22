from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lib.config import discover_configs
from lib.errors import BuildToolError
from lib.feature_evidence import validate_feature_evidence


class FeatureEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _, _, cls.profiles, cls.features = discover_configs(ROOT)
        cls.contract = json.loads(
            (ROOT / "configs" / "feature-evidence.yml").read_text(encoding="utf-8")
        )

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_contract(self, document: dict[str, object], name: str) -> Path:
        path = Path(self.temporary.name) / name
        path.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return path

    def _copy_contract(self) -> dict[str, object]:
        return json.loads(json.dumps(self.contract))

    def test_contract_covers_and_validates_the_complete_catalog(self) -> None:
        catalogs = {frozenset(feature.flags) for feature in self.features.values()}
        self.assertEqual(len(catalogs), 1)
        catalog = next(iter(catalogs))
        self.assertEqual(len(catalog), 52)
        self.assertEqual(
            set(self.contract["feature_flags"]),
            set(catalog),
        )

        report = validate_feature_evidence(ROOT, self.profiles, self.features)
        self.assertEqual(report["catalog_size"], 52)
        self.assertEqual(
            report["selection_semantics"],
            "profile-base-capability",
        )
        self.assertEqual(report["profile_base_combinations"], 9)
        self.assertGreater(report["enabled_checks"], 0)

    def test_true_patch_feature_without_a_selected_operation_fails(self) -> None:
        for flag in ("performance.io", "performance.scheduler"):
            with self.subTest(flag=flag):
                nethunter = self.features["nethunter"]
                flags = dict(nethunter.flags)
                flags[flag] = True
                changed = dict(self.features)
                changed["nethunter"] = replace(nethunter, flags=flags)
                with self.assertRaisesRegex(
                    BuildToolError,
                    rf"nethunter/oos15-cn enables {re.escape(flag)} "
                    "but selects no declared evidence",
                ):
                    validate_feature_evidence(ROOT, self.profiles, changed)

    def test_true_kconfig_feature_without_a_selected_request_fails(self) -> None:
        nethunter = self.features["nethunter"]
        flags = dict(nethunter.flags)
        flags["network.qdisc_cake"] = True
        changed = dict(self.features)
        changed["nethunter"] = replace(nethunter, flags=flags)
        with self.assertRaisesRegex(
            BuildToolError,
            r"nethunter/oos15-cn enables network\.qdisc_cake "
            "but selects no declared evidence",
        ):
            validate_feature_evidence(ROOT, self.profiles, changed)

    def test_enabled_flag_with_an_empty_contract_fails(self) -> None:
        document = self._copy_contract()
        document["feature_flags"]["network.bbr"] = []
        path = self._write_contract(document, "empty-evidence.yml")
        with self.assertRaisesRegex(
            BuildToolError,
            r"full/oos15-cn enables network\.bbr but selects no declared evidence",
        ):
            validate_feature_evidence(
                ROOT,
                self.profiles,
                self.features,
                evidence_path=path,
            )

    def test_unknown_patch_and_kconfig_references_fail(self) -> None:
        mutations = (
            (
                "unknown-operation.yml",
                "performance.io",
                {"kind": "patch-operation", "operation": "wild:not-a-real-operation"},
                "unknown patch operation reference",
            ),
            (
                "unknown-kconfig.yml",
                "network.bbr",
                {
                    "kind": "kconfig-request",
                    "symbol": "CONFIG_NOT_A_REAL_OP13_SYMBOL",
                    "value": "y",
                },
                "unknown Kconfig request reference",
            ),
            (
                "unknown-module.yml",
                "nethunter.wifi_rtw88",
                {"kind": "external-module", "dependency": "not-a-real-module"},
                "unknown external module reference",
            ),
        )
        for filename, flag, evidence, message in mutations:
            with self.subTest(flag=flag):
                document = self._copy_contract()
                document["feature_flags"][flag] = [evidence]
                path = self._write_contract(document, filename)
                with self.assertRaisesRegex(BuildToolError, message):
                    validate_feature_evidence(
                        ROOT,
                        self.profiles,
                        self.features,
                        evidence_path=path,
                    )

    def test_missing_and_unknown_catalog_keys_fail(self) -> None:
        for name, mutate, message in (
            (
                "missing-key.yml",
                lambda flags: flags.pop("network.bbr"),
                "catalog mismatch.*missing: network.bbr",
            ),
            (
                "unknown-key.yml",
                lambda flags: flags.update({"network.not_real": []}),
                "catalog mismatch.*unknown: network.not_real",
            ),
        ):
            with self.subTest(name=name):
                document = self._copy_contract()
                mutate(document["feature_flags"])
                path = self._write_contract(document, name)
                with self.assertRaisesRegex(BuildToolError, message):
                    validate_feature_evidence(
                        ROOT,
                        self.profiles,
                        self.features,
                        evidence_path=path,
                    )

    def test_selection_semantics_are_explicit_and_fixed(self) -> None:
        document = self._copy_contract()
        document["selection_semantics"] = "per-root-activation"
        path = self._write_contract(document, "wrong-semantics.yml")
        with self.assertRaisesRegex(
            BuildToolError,
            "selection_semantics must be 'profile-base-capability'",
        ):
            validate_feature_evidence(
                ROOT,
                self.profiles,
                self.features,
                evidence_path=path,
            )


if __name__ == "__main__":
    unittest.main()
