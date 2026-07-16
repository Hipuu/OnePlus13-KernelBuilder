from __future__ import annotations

import importlib.util
import json
import re
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import ModuleType
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lib.config import discover_configs  # noqa: E402
from lib.patches import (  # noqa: E402
    _expand_kernel_tree_operation,
    _load_series,
    _operation_enabled,
    _series_paths,
)


def _load_helper() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "oneplus_build_cleanup_wiring_contract",
        ROOT / "scripts" / "integrate-oneplus-build-cleanup.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HELPER = _load_helper()
CLEANUP_ID = "common:oneplus-build-restore-only-gki-headers"


def _selected_operations(feature: Any, base: str, root_variant: str) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for path in _series_paths(ROOT, feature, root_variant):
        series_id, operations = _load_series(path)
        for operation in operations:
            if not _operation_enabled(operation, feature, base, root_variant):
                continue
            qualified = dict(operation)
            qualified["id"] = f"{series_id}:{operation['id']}"
            selected.extend(
                _expand_kernel_tree_operation(
                    qualified,
                    f"cleanup wiring contract {qualified['id']}",
                )
            )
    return selected


def _mentions_vendor_tree(operation: dict[str, Any]) -> bool:
    if operation.get("kernel_tree") == "msm-kernel":
        return True
    return "kernel_platform/msm-kernel" in json.dumps(operation, sort_keys=True)


class OnePlusBuildCleanupWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _, _, cls.profiles, cls.features = discover_configs(ROOT)
        cls.common = json.loads(
            (ROOT / "patches" / "series" / "common.yml").read_text(encoding="utf-8")
        )

    def test_common_series_wires_the_exact_helper_first(self) -> None:
        operation = self.common["operations"][0]
        self.assertEqual(operation["id"], "oneplus-build-restore-only-gki-headers")
        self.assertEqual(operation["type"], "exec")
        self.assertEqual(operation["cwd"], ".")
        self.assertEqual(
            operation["argv"],
            [
                "python3",
                "{repo_root}/scripts/integrate-oneplus-build-cleanup.py",
                "--source-dir",
                "{source_dir}/kernel_platform/msm-kernel",
                "--base",
                "{base}",
            ],
        )
        self.assertEqual(
            operation["expected_outputs"],
            ["kernel_platform/msm-kernel/.op13-build-with-bazel-cleanup.json"],
        )
        for feature in self.features.values():
            first = feature.patch_series[0]
            self.assertEqual(first, "patches/series/common.yml")

    def test_exact_helper_contracts_match_all_locked_vendor_commits(self) -> None:
        self.assertEqual(set(HELPER.PROFILE_CONTRACTS), set(self.profiles))
        for base, contract in HELPER.PROFILE_CONTRACTS.items():
            manifest = ET.parse(ROOT / "manifests" / "lockfiles" / f"{base}.xml")
            vendor_projects = [
                project
                for project in manifest.getroot().findall("project")
                if project.get("path") == "kernel_platform/msm-kernel"
            ]
            self.assertEqual(len(vendor_projects), 1)
            self.assertEqual(vendor_projects[0].get("revision"), contract["commit"])
            for field in ("pre_sha256", "post_sha256"):
                self.assertRegex(contract[field], r"^[0-9a-f]{64}$")

        self.assertEqual(
            HELPER.PROFILE_CONTRACTS["oos15-cn"]["pre_sha256"],
            HELPER.PROFILE_CONTRACTS["oos15-global"]["pre_sha256"],
        )
        self.assertNotEqual(
            HELPER.PROFILE_CONTRACTS["oos15-global"]["pre_sha256"],
            HELPER.PROFILE_CONTRACTS["oos16"]["pre_sha256"],
        )

    def test_cleanup_gate_survives_root_prelude_and_precedes_vendor_feature_patches(self) -> None:
        forbidden_before_cleanup = (
            "build_with_bazel.py",
            "integrate-oneplus-build-cleanup.py",
            ".op13-build-with-bazel-cleanup.json",
        )
        for feature in self.features.values():
            for base in self.profiles:
                no_root = _selected_operations(feature, base, "none")
                self.assertEqual(no_root[0]["id"], CLEANUP_ID)

                for root_variant in feature.root_variants:
                    with self.subTest(
                        feature=feature.id,
                        base=base,
                        root_variant=root_variant,
                    ):
                        operations = _selected_operations(feature, base, root_variant)
                        ids = [str(operation["id"]) for operation in operations]
                        self.assertEqual(ids.count(CLEANUP_ID), 1)
                        cleanup_index = ids.index(CLEANUP_ID)

                        # Root integration is intentionally prepended. It may
                        # change other msm-kernel files, but it must leave the
                        # exact wrapper preimage untouched.
                        preceding = operations[:cleanup_index]
                        self.assertTrue(preceding)
                        self.assertTrue(
                            all(str(operation["id"]).startswith("root:") for operation in preceding)
                        )
                        preceding_text = json.dumps(preceding, sort_keys=True)
                        for forbidden in forbidden_before_cleanup:
                            self.assertNotIn(forbidden, preceding_text)
                        self.assertTrue(any(_mentions_vendor_tree(item) for item in preceding))

                        common_vendor = [
                            index
                            for index, operation in enumerate(operations)
                            if index != cleanup_index
                            and str(operation["id"]).startswith("common:")
                            and _mentions_vendor_tree(operation)
                        ]
                        self.assertTrue(common_vendor)
                        self.assertTrue(all(index > cleanup_index for index in common_vendor))

                        wild_vendor = [
                            index
                            for index, operation in enumerate(operations)
                            if str(operation["id"]).startswith("wild:")
                            and _mentions_vendor_tree(operation)
                        ]
                        self.assertTrue(all(index > cleanup_index for index in wild_vendor))

                        if feature.flags.get("oneplus.hmbird_fengchi_scx", False):
                            hmbird_id = "wild:hmbird-fengchi-oneplus13-vendor"
                            self.assertIn(hmbird_id, ids)
                            self.assertGreater(ids.index(hmbird_id), cleanup_index)

    def test_actions_apply_cleanup_before_configure_and_official_build(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "build.yml").read_text(
            encoding="utf-8"
        )
        apply_index = workflow.index("- name: Apply selected patch series")
        configure_index = workflow.index("- name: Configure kernel and modules")
        build_index = workflow.index("- name: Build kernel")
        self.assertLess(apply_index, configure_index)
        self.assertLess(configure_index, build_index)

        apply_block = workflow[apply_index:configure_index]
        configure_block = workflow[configure_index:build_index]
        build_block = workflow[build_index:]
        self.assertRegex(apply_block, re.escape("bash scripts/apply-series.sh"))
        self.assertRegex(configure_block, re.escape("bash scripts/configure.sh"))
        self.assertRegex(build_block, re.escape("bash scripts/build-kernel.sh"))


if __name__ == "__main__":
    unittest.main()
