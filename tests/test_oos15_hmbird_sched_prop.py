from __future__ import annotations

import dataclasses
import importlib.util
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lib.config import discover_configs  # noqa: E402
from lib.patches import _load_series, _operation_enabled  # noqa: E402


SPEC = importlib.util.spec_from_file_location(
    "integrate_oos15_hmbird_sched_prop",
    ROOT / "scripts" / "integrate-oos15-hmbird-sched-prop.py",
)
assert SPEC is not None and SPEC.loader is not None
HELPER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = HELPER
SPEC.loader.exec_module(HELPER)

VENDOR_SPEC = importlib.util.spec_from_file_location(
    "integrate_vendor_hmbird_for_sched_prop",
    ROOT / "scripts" / "integrate-vendor-hmbird.py",
)
assert VENDOR_SPEC is not None and VENDOR_SPEC.loader is not None
VENDOR = importlib.util.module_from_spec(VENDOR_SPEC)
sys.modules[VENDOR_SPEC.name] = VENDOR
VENDOR_SPEC.loader.exec_module(VENDOR)


class Oos15HmbirdSchedPropTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.source = Path(self.temporary.name) / "source"
        (self.source / HELPER.STAMP_RELATIVE.parent).mkdir(parents=True)
        self.targets = {}
        for tree, relative in HELPER.TREE_RELATIVES.items():
            target = self.source / relative / HELPER.TARGET_RELATIVE
            target.parent.mkdir(parents=True)
            target.write_bytes(HELPER.PREIMAGE)
            target.chmod(0o755)
            self.targets[tree] = target
        self.mode = stat.S_IMODE(self.targets["common"].stat().st_mode)
        self.contract = dataclasses.replace(HELPER.CONTRACT, mode=self.mode)
        self._write_vendor_stamp("oos15-cn")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_vendor_stamp(self, base: str, **input_overrides: str) -> None:
        expected = self.contract.main_patches[base]
        inputs = {
            "vendor_commit": self.contract.vendor_commits[base],
            "wild_commit": self.contract.wild_commit,
            "main_patch": expected["path"],
            "main_patch_sha256": expected["sha256"],
            "compatibility_patch": self.contract.compatibility_patch,
            "compatibility_patch_sha256": self.contract.compatibility_sha256,
            **input_overrides,
        }
        path = self.source / HELPER.VENDOR_STAMP_RELATIVE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "integration": "oneplus-vendor-hmbird-fengchi",
                    "base": base,
                    "inputs": inputs,
                }
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )

    def test_exact_headers_are_rewritten_together_and_stamped(self) -> None:
        document = HELPER.integrate(
            self.source,
            "oos15-cn",
            contract=self.contract,
        )

        self.assertEqual(set(document["targets"]), {"common", "msm-kernel"})
        self.assertEqual(document["base"], "oos15-cn")
        self.assertEqual(
            document["inputs"]["wild_commit"], self.contract.wild_commit
        )
        for tree, target in self.targets.items():
            with self.subTest(tree=tree):
                self.assertEqual(target.read_bytes(), HELPER.POSTIMAGE)
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), self.mode)
                record = document["targets"][tree]
                self.assertEqual(record["pre_blob"], self.contract.pre_blob)
                self.assertEqual(record["post_blob"], self.contract.post_blob)
                self.assertEqual(record["pre_sha256"], self.contract.pre_sha256)
                self.assertEqual(record["post_sha256"], self.contract.post_sha256)
        stamp = self.source / HELPER.STAMP_RELATIVE
        self.assertEqual(json.loads(stamp.read_text(encoding="utf-8")), document)

    def test_global_uses_the_same_exact_two_tree_transaction(self) -> None:
        self._write_vendor_stamp("oos15-global")
        document = HELPER.integrate(
            self.source,
            "oos15-global",
            contract=self.contract,
        )
        self.assertEqual(document["base"], "oos15-global")
        self.assertEqual(
            document["inputs"]["vendor_commit"],
            self.contract.vendor_commits["oos15-global"],
        )
        for target in self.targets.values():
            self.assertEqual(target.read_bytes(), HELPER.POSTIMAGE)

    def test_preimage_mode_vendor_stamp_and_base_are_fail_closed(self) -> None:
        before = {tree: target.read_bytes() for tree, target in self.targets.items()}
        with self.assertRaisesRegex(HELPER.IntegrationError, "only valid"):
            HELPER.integrate(self.source, "oos16", contract=self.contract)

        for tree, target in self.targets.items():
            with self.subTest(corrupt_tree=tree):
                target.write_bytes(b"X" + HELPER.PREIMAGE[1:])
                with self.assertRaisesRegex(HELPER.IntegrationError, "preimage changed"):
                    HELPER.integrate(self.source, "oos15-cn", contract=self.contract)
                target.write_bytes(before[tree])

        wrong_mode = dataclasses.replace(
            self.contract,
            mode=self.mode ^ stat.S_IXUSR,
        )
        with self.assertRaisesRegex(HELPER.IntegrationError, "mode changed"):
            HELPER.integrate(self.source, "oos15-cn", contract=wrong_mode)

        self._write_vendor_stamp("oos15-cn", wild_commit="0" * 40)
        with self.assertRaisesRegex(HELPER.IntegrationError, "stamp does not match"):
            HELPER.integrate(self.source, "oos15-cn", contract=self.contract)

        for tree, target in self.targets.items():
            self.assertEqual(target.read_bytes(), before[tree])
        self.assertFalse((self.source / HELPER.STAMP_RELATIVE).exists())

    def test_stamp_failure_restores_both_full_preimages_and_modes(self) -> None:
        with mock.patch.object(
            HELPER,
            "_atomic_json",
            side_effect=RuntimeError("forced stamp failure"),
        ), self.assertRaisesRegex(RuntimeError, "forced stamp failure"):
            HELPER.integrate(
                self.source,
                "oos15-cn",
                contract=self.contract,
            )

        for target in self.targets.values():
            self.assertEqual(target.read_bytes(), HELPER.PREIMAGE)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), self.mode)
        self.assertFalse((self.source / HELPER.STAMP_RELATIVE).exists())

    def test_real_contract_is_cross_locked_to_both_wild_oos15_patches(self) -> None:
        lock = json.loads((ROOT / "dependencies" / "lock.yml").read_text())
        self.assertEqual(
            HELPER.CONTRACT.wild_commit,
            lock["dependencies"]["wild_kernel_patches"]["commit"],
        )
        self.assertEqual(HELPER.CONTRACT.bases, ("oos15-cn", "oos15-global"))
        self.assertEqual(HELPER.CONTRACT.mode, 0o755)
        for base in HELPER.CONTRACT.bases:
            with self.subTest(base=base):
                vendor = VENDOR.BASE_SPECS[base]
                expected = HELPER.CONTRACT.main_patches[base]
                self.assertEqual(
                    HELPER.CONTRACT.vendor_commits[base], vendor.vendor_commit
                )
                self.assertEqual(expected["path"], vendor.main_patch.as_posix())
                self.assertEqual(expected["sha256"], vendor.main_sha256)
                self.assertEqual(
                    HELPER.CONTRACT.compatibility_patch,
                    vendor.compatibility_patch.as_posix(),
                )
                self.assertEqual(
                    HELPER.CONTRACT.compatibility_sha256,
                    vendor.compatibility_sha256,
                )

        self.assertEqual(len(HELPER.PREIMAGE), 1776)
        self.assertEqual(len(HELPER.POSTIMAGE), 1788)
        self.assertEqual(
            HELPER.sha256_bytes(HELPER.PREIMAGE),
            "a013e0caa83a1ec80efd0b0c5f6cca06aeefa0a4449e5bc678ec95392de64b84",
        )
        self.assertEqual(
            HELPER.git_blob_oid(HELPER.PREIMAGE),
            "9f1afdb8a04b183e12ea882633839bd045f54934",
        )
        self.assertEqual(
            HELPER.sha256_bytes(HELPER.POSTIMAGE),
            "5727cadb1e7293bc7e78e159135f9e83bd0648cbd6c056a219168da00777c803",
        )
        self.assertEqual(
            HELPER.git_blob_oid(HELPER.POSTIMAGE),
            "f243c05334836b5b7340c4f45fc76f91a15714fa",
        )
        self.assertEqual(HELPER.PREIMAGE.count(HELPER.OLD_ACCESS), 3)
        self.assertEqual(HELPER.POSTIMAGE.count(HELPER.OLD_ACCESS), 0)
        self.assertEqual(HELPER.POSTIMAGE.count(HELPER.NEW_DECLARATION), 3)
        self.assertEqual(HELPER.POSTIMAGE.count(HELPER.NEW_ACCESS), 3)


class Oos15HmbirdSchedPropWiringTests(unittest.TestCase):
    def test_operation_is_oos15_hmbird_only_and_immediately_post_vendor(self) -> None:
        series_id, operations = _load_series(ROOT / "patches" / "series" / "wild.yml")
        self.assertEqual(series_id, "wild")
        operation_by_id = {operation["id"]: operation for operation in operations}
        operation = operation_by_id["hmbird-sched-prop-oos15"]
        self.assertEqual(operation["type"], "exec")
        self.assertEqual(operation["cwd"], ".")
        self.assertEqual(operation["bases"], ["oos15-cn", "oos15-global"])
        self.assertEqual(operation["feature"], "oneplus.hmbird_fengchi_scx")
        self.assertEqual(
            operation["argv"],
            [
                "python3",
                "{repo_root}/scripts/integrate-oos15-hmbird-sched-prop.py",
                "--source-dir",
                "{source_dir}",
                "--base",
                "{base}",
            ],
        )
        self.assertEqual(
            operation["expected_outputs"],
            [".op13/oos15-hmbird-sched-prop.json"],
        )
        ids = [item["id"] for item in operations]
        self.assertEqual(
            ids.index(operation["id"]),
            ids.index("hmbird-fengchi-oneplus13-vendor") + 1,
        )
        self.assertLess(
            ids.index(operation["id"]),
            ids.index("hmbird-sched-assist-oos15-cn"),
        )

    def test_selection_is_limited_to_full_and_wild_oos15_builds(self) -> None:
        _, _, profiles, features = discover_configs(ROOT)
        _, operations = _load_series(ROOT / "patches" / "series" / "wild.yml")
        operation = next(
            item for item in operations if item["id"] == "hmbird-sched-prop-oos15"
        )
        enabled = {
            (feature.id, base)
            for feature in features.values()
            for base in profiles
            if _operation_enabled(operation, feature, base, "kernelsu-next")
        }
        self.assertEqual(
            enabled,
            {
                (feature, base)
                for feature in ("full", "wild")
                for base in ("oos15-cn", "oos15-global")
            },
        )


if __name__ == "__main__":
    unittest.main()
