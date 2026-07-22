from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lib.config import discover_configs
from lib.context import new_context, write_context
from lib.errors import BuildToolError
from lib.patches import (
    _expand_kernel_tree_operation,
    _load_series,
    _validate_exec_argv_placeholders,
    apply_patch_series,
)
from tests.support import make_repository, write_json


class KernelTreeFanoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = make_repository(Path(self.temporary.name))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _series(self, operations: list[dict[str, object]]) -> Path:
        path = self.root / "patches" / "series" / "fanout.yml"
        write_json(
            path,
            {
                "schema_version": 1,
                "id": "fanout",
                "operations": operations,
            },
        )
        return path

    def _source_context(self) -> tuple[Path, Path, object, object, object]:
        _, lock, profiles, features = discover_configs(self.root)
        source = self.root / "out" / "source"
        for tree in ("common", "msm-kernel"):
            target = source / "kernel_platform" / tree / "fixture.txt"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("before\n", encoding="utf-8", newline="\n")
        resolved = source / ".op13" / "resolved.xml"
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_bytes(profiles["oos16"].locked_manifest.read_bytes())
        context_path = source / ".op13" / "build-context.json"
        write_context(
            context_path,
            new_context(profiles["oos16"], lock, resolved, smoke=False),
        )
        return source, context_path, lock, profiles["oos16"], features["test"]

    def test_fanout_has_canonical_ids_and_operation_major_order(self) -> None:
        source, context_path, lock, profile, feature = self._source_context()
        series = self.root / "patches" / "series" / "test.yml"
        data = json.loads(series.read_text(encoding="utf-8"))
        data["operations"] = [
            {
                "id": "replace-token",
                "type": "replace",
                "kernel_trees": ["msm-kernel", "common"],
                "target": "kernel_platform/{kernel_tree}/fixture.txt",
                "find": "before",
                "replace": "after",
                "count": 1,
            },
            {
                "id": "append-token",
                "type": "append",
                "kernel_trees": ["msm-kernel", "common"],
                "target": "kernel_platform/{kernel_tree}/fixture.txt",
                "lines": ["tail"],
            },
        ]
        write_json(series, data)

        records = apply_patch_series(
            root=self.root,
            source_dir=source,
            cache_root=self.root / ".cache" / "op13",
            context_path=context_path,
            profile=profile,
            feature=feature,
            lock=lock,
            root_variant="none",
            check_only=False,
            smoke=False,
            log_dir=self.root / "out" / "debug",
        )

        self.assertEqual(
            [record["id"] for record in records],
            [
                "test:replace-token@common",
                "test:replace-token@msm-kernel",
                "test:append-token@common",
                "test:append-token@msm-kernel",
            ],
        )
        self.assertEqual(
            [record["kernel_tree"] for record in records],
            ["common", "msm-kernel", "common", "msm-kernel"],
        )
        for tree in ("common", "msm-kernel"):
            target = source / "kernel_platform" / tree / "fixture.txt"
            self.assertEqual(target.read_text(encoding="utf-8"), "after\ntail\n")

    def test_all_supported_fields_are_substituted(self) -> None:
        operation = {
            "id": "fanout:fields",
            "type": "exec",
            "optional": False,
            "kernel_trees": ["common"],
            "cwd": "kernel_platform/{kernel_tree}",
            "target": "kernel_platform/{kernel_tree}/target",
            "destination": "kernel_platform/{kernel_tree}/destination",
            "argv": ["tool", "kernel_platform/{kernel_tree}/argument"],
            "expected_outputs": ["kernel_platform/{kernel_tree}/output"],
        }

        [expanded] = _expand_kernel_tree_operation(operation)

        self.assertEqual(expanded["id"], "fanout:fields@common")
        self.assertEqual(expanded["kernel_tree"], "common")
        self.assertEqual(expanded["cwd"], "kernel_platform/common")
        self.assertEqual(expanded["target"], "kernel_platform/common/target")
        self.assertEqual(
            expanded["destination"],
            "kernel_platform/common/destination",
        )
        self.assertEqual(
            expanded["argv"],
            ["tool", "kernel_platform/common/argument"],
        )
        self.assertEqual(
            expanded["expected_outputs"],
            ["kernel_platform/common/output"],
        )
        self.assertNotIn("kernel_trees", expanded)

    def test_duplicate_kernel_trees_are_rejected(self) -> None:
        path = self._series(
            [
                {
                    "id": "bad",
                    "type": "replace",
                    "kernel_trees": ["common", "common"],
                    "target": "kernel_platform/{kernel_tree}/fixture.txt",
                }
            ]
        )
        with self.assertRaisesRegex(BuildToolError, "entries must be unique"):
            _load_series(path)

    def test_unknown_kernel_tree_is_rejected(self) -> None:
        path = self._series(
            [
                {
                    "id": "bad",
                    "type": "replace",
                    "kernel_trees": ["common", "vendor"],
                    "target": "kernel_platform/{kernel_tree}/fixture.txt",
                }
            ]
        )
        with self.assertRaisesRegex(BuildToolError, "unknown kernel trees"):
            _load_series(path)

    def test_empty_kernel_tree_list_is_rejected(self) -> None:
        path = self._series(
            [
                {
                    "id": "bad",
                    "type": "replace",
                    "kernel_trees": [],
                    "target": "kernel_platform/{kernel_tree}/fixture.txt",
                }
            ]
        )
        with self.assertRaisesRegex(BuildToolError, "non-empty array"):
            _load_series(path)

    def test_placeholder_without_fanout_is_rejected(self) -> None:
        path = self._series(
            [
                {
                    "id": "bad",
                    "type": "replace",
                    "target": "kernel_platform/{kernel_tree}/fixture.txt",
                }
            ]
        )
        with self.assertRaisesRegex(BuildToolError, r"unresolved \{kernel_tree\}"):
            _load_series(path)

    def test_placeholder_in_unsupported_field_is_rejected(self) -> None:
        path = self._series(
            [
                {
                    "id": "bad",
                    "type": "apply",
                    "kernel_trees": ["common", "msm-kernel"],
                    "path": "patches/{kernel_tree}.patch",
                }
            ]
        )
        with self.assertRaisesRegex(BuildToolError, "supported only"):
            _load_series(path)

    def test_fanout_without_placeholder_is_rejected(self) -> None:
        path = self._series(
            [
                {
                    "id": "bad",
                    "type": "replace",
                    "kernel_trees": ["common", "msm-kernel"],
                    "target": "fixture.txt",
                }
            ]
        )
        with self.assertRaisesRegex(BuildToolError, "requires at least one"):
            _load_series(path)

    def test_exec_placeholder_validation_rejects_typos_and_undeclared_dependencies(
        self,
    ) -> None:
        with self.assertRaisesRegex(BuildToolError, "unsupported argv placeholder"):
            _validate_exec_argv_placeholders(
                ["{dependency:wild_kernel_patches}"],
                {"wild_kernel_patches"},
                "fixture",
            )
        with self.assertRaisesRegex(BuildToolError, "is not declared"):
            _validate_exec_argv_placeholders(
                ["{dependency_dir:wild_kernel_patches}"],
                set(),
                "fixture",
            )
        _validate_exec_argv_placeholders(
            [
                "{repo_root}/script.py",
                "{source_dir}/kernel_platform/msm-kernel",
                "{dependency_dir:wild_kernel_patches}",
                "{base}",
            ],
            {"wild_kernel_patches"},
            "fixture",
        )


class KernelTreeManifestContractTests(unittest.TestCase):
    def test_common_and_kernel_facing_root_operations_use_dual_tree_fanout(self) -> None:
        common = json.loads(
            (ROOT / "patches" / "series" / "common.yml").read_text(encoding="utf-8")
        )
        platform_operations = {
            "oneplus-build-restore-only-gki-headers",
            "kmi-symbol-list-vendor-module-closure",
            "kmi-symbol-list-wireless-led-closure",
        }
        for operation in common["operations"]:
            if operation["id"] in platform_operations:
                self.assertNotIn("kernel_trees", operation)
                self.assertNotIn("{kernel_tree}", json.dumps(operation))
                continue
            self.assertEqual(operation.get("kernel_trees"), ["common", "msm-kernel"])
            self.assertIn("{kernel_tree}", json.dumps(operation))

        root = json.loads(
            (ROOT / "patches" / "series" / "root.yml").read_text(encoding="utf-8")
        )
        fanout_ids = {
            "patch-common-susfs",
            "install-kernelsu-driver",
            "install-kernelsu-next-driver",
            "register-kernelsu-kconfig",
            "register-kernelsu-makefile",
        }
        single_run_ids = {
            "stage-kernelsu",
            "stage-kernelsu-next",
            "patch-kernelsu-susfs",
            "fix-classic-kernelsu-susfs-direct-wrapper-calls",
            "patch-kernelsu-next-susfs",
            "pin-kernelsu-version",
            "pin-kernelsu-next-version",
        }
        operations = {operation["id"]: operation for operation in root["operations"]}
        self.assertEqual(set(operations), fanout_ids | single_run_ids)
        for operation_id in fanout_ids:
            operation = operations[operation_id]
            self.assertEqual(operation.get("kernel_trees"), ["common", "msm-kernel"])
            self.assertIn("{kernel_tree}", json.dumps(operation))
        for operation_id in single_run_ids:
            operation = operations[operation_id]
            self.assertNotIn("kernel_trees", operation)
            self.assertNotIn("{kernel_tree}", json.dumps(operation))

    def test_wild_vendor_compatible_operations_use_dual_tree_fanout(self) -> None:
        wild = json.loads(
            (ROOT / "patches" / "series" / "wild.yml").read_text(encoding="utf-8")
        )
        operations = {operation["id"]: operation for operation in wild["operations"]}
        dual_tree_ids = {
            "hmbird-device-tree-overwriter",
            "hmbird-ogki-device-tree-config",
            "module-overlay-source-only",
            "ntsync-android15-6.6-compat",
            "ntsync-implementation",
            "unicode-normalization-bypass-fix",
            "disable-cache-hot-buddy",
            "increase-socket-memory-packets",
            "cpufreq-minimum-limit-hook",
        }
        for operation_id in dual_tree_ids:
            operation = operations[operation_id]
            self.assertEqual(operation.get("kernel_trees"), ["common", "msm-kernel"])
            self.assertIn("{kernel_tree}", json.dumps(operation))

        vendor = operations["hmbird-fengchi-oneplus13-vendor"]
        self.assertEqual(vendor["type"], "exec")
        self.assertEqual(vendor["dependency"], "wild_kernel_patches")
        self.assertIn(
            "{source_dir}/kernel_platform/msm-kernel",
            vendor["argv"],
        )
        self.assertIn(
            "{source_dir}/kernel_platform/common",
            vendor["argv"],
        )
        self.assertIn(
            "{dependency_dir:wild_kernel_patches}",
            vendor["argv"],
        )
        self.assertEqual(
            vendor["expected_outputs"],
            ["kernel_platform/msm-kernel/.op13-hmbird-vendor.json"],
        )

        common_main = operations["hmbird-fengchi-oneplus13"]
        self.assertNotIn("kernel_trees", common_main)
        self.assertEqual(common_main["cwd"], "kernel_platform/common")


if __name__ == "__main__":
    unittest.main()
