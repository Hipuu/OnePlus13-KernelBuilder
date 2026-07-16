from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
import json
import stat
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lib.config import discover_configs  # noqa: E402
from lib.patches import _load_series, _operation_enabled  # noqa: E402


SPEC = importlib.util.spec_from_file_location(
    "integrate_oos15_cn_hmbird_overlay",
    ROOT / "scripts" / "integrate-oos15-cn-hmbird-overlay.py",
)
assert SPEC is not None and SPEC.loader is not None
HELPER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = HELPER
SPEC.loader.exec_module(HELPER)


class Oos15CnHmbirdOverlayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.source = Path(self.temporary.name) / "modules"
        self.target = self.source / HELPER.TARGET_RELATIVE
        self.before = b"fixture-prefix\n" + HELPER.OLD_FRAGMENT + b"fixture-suffix\n"
        self.after = b"fixture-prefix\n" + HELPER.NEW_FRAGMENT + b"fixture-suffix\n"
        self.target.parent.mkdir(parents=True)
        (self.source / HELPER.STAMP_RELATIVE.parent).mkdir(parents=True)
        self.target.write_bytes(self.before)
        self.target.chmod(0o755)
        self._git("init", "-q")
        self._git("config", "user.name", "Fixture")
        self._git("config", "user.email", "fixture@example.invalid")
        self._git("config", "core.autocrlf", "false")
        self._git("add", ".")
        self._git("commit", "-q", "-m", "fixture")
        source_commit = self._git("rev-parse", "HEAD")
        target_blob = self._git(
            "rev-parse", f"HEAD:{HELPER.TARGET_RELATIVE.as_posix()}"
        )
        self.mode = stat.S_IMODE(self.target.stat().st_mode)
        self.contract = HELPER.OverlayContract(
            base="fixture-cn",
            source_commit=source_commit,
            target_blob=target_blob,
            post_blob=HELPER.git_blob_oid(self.after),
            pre_sha256=hashlib.sha256(self.before).hexdigest(),
            post_sha256=hashlib.sha256(self.after).hexdigest(),
            pre_size=len(self.before),
            post_size=len(self.after),
            mode=self.mode,
            peer_sources={},
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _git(self, *arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.source), *arguments],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return result.stdout.strip()

    def test_exact_pinned_overlay_is_rewritten_and_stamped(self) -> None:
        document = HELPER.integrate(
            self.source,
            "fixture-cn",
            contract=self.contract,
        )

        self.assertEqual(self.target.read_bytes(), self.after)
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), self.mode)
        self.assertEqual(document["source_commit"], self.contract.source_commit)
        self.assertEqual(document["target"]["blob"], self.contract.target_blob)
        self.assertEqual(document["target"]["post_blob"], self.contract.post_blob)
        self.assertEqual(document["target"]["pre_sha256"], self.contract.pre_sha256)
        self.assertEqual(document["target"]["post_sha256"], self.contract.post_sha256)
        self.assertEqual(document["target"]["pre_size"], len(self.before))
        self.assertEqual(document["target"]["post_size"], len(self.after))
        self.assertEqual(document["target"]["mode"], f"{self.mode:04o}")
        stamp = self.source / HELPER.STAMP_RELATIVE
        self.assertEqual(json.loads(stamp.read_text(encoding="utf-8")), document)
        for token in HELPER.FORBIDDEN_POSTIMAGE_TOKENS:
            self.assertNotIn(token, self.target.read_bytes())

    def test_wrong_commit_blob_mode_and_worktree_stop_before_mutation(self) -> None:
        contracts = (
            dataclasses.replace(self.contract, source_commit="0" * 40),
            dataclasses.replace(self.contract, target_blob="1" * 40),
            dataclasses.replace(self.contract, mode=self.mode ^ stat.S_IXUSR),
        )
        for contract in contracts:
            with self.subTest(contract=contract), self.assertRaises(HELPER.IntegrationError):
                HELPER.integrate(self.source, "fixture-cn", contract=contract)
            self.assertEqual(self.target.read_bytes(), self.before)
            self.assertFalse((self.source / HELPER.STAMP_RELATIVE).exists())

        self.target.write_bytes(b"F" + self.before[1:])
        with self.assertRaisesRegex(HELPER.IntegrationError, "preimage changed"):
            HELPER.integrate(self.source, "fixture-cn", contract=self.contract)
        self.assertEqual(self.target.read_bytes(), b"F" + self.before[1:])
        self.assertFalse((self.source / HELPER.STAMP_RELATIVE).exists())

    def test_wrong_base_and_already_applied_state_are_rejected(self) -> None:
        with self.assertRaisesRegex(HELPER.IntegrationError, "only valid"):
            HELPER.integrate(self.source, "other", contract=self.contract)
        self.target.write_bytes(self.after)
        with self.assertRaisesRegex(HELPER.IntegrationError, "already integrated"):
            HELPER.integrate(self.source, "fixture-cn", contract=self.contract)
        self.assertEqual(self.target.read_bytes(), self.after)

    def test_stamp_failure_restores_full_preimage_and_mode(self) -> None:
        with mock.patch.object(
            HELPER,
            "_atomic_json",
            side_effect=RuntimeError("forced stamp failure"),
        ), self.assertRaisesRegex(RuntimeError, "forced stamp failure"):
            HELPER.integrate(self.source, "fixture-cn", contract=self.contract)

        self.assertEqual(self.target.read_bytes(), self.before)
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), self.mode)
        self.assertFalse((self.source / HELPER.STAMP_RELATIVE).exists())

    def test_real_contract_records_the_exact_oneplus_preimage_and_peer_cleanup(self) -> None:
        self.assertEqual(HELPER.CONTRACT.base, "oos15-cn")
        self.assertEqual(
            HELPER.CONTRACT.source_commit,
            "a85bac41e21a790e216039cde1d34a6c5d6416d1",
        )
        self.assertEqual(
            HELPER.CONTRACT.target_blob,
            "625b526e0c234212152b46a0e5b874368f5a3902",
        )
        self.assertEqual(
            HELPER.CONTRACT.post_blob,
            "6e138d4b1903b361f507d40ad1a01a6f1fdcc514",
        )
        self.assertEqual(
            HELPER.CONTRACT.pre_sha256,
            "96b1a2cfe793bc33f1e6c942058767587d95ff4317b8811a305855fd570123af",
        )
        self.assertEqual(
            HELPER.CONTRACT.post_sha256,
            "df731638c1e525b2ae330fa36738f80f48b24d09a1d099e21f314b4ca005dd63",
        )
        self.assertEqual(HELPER.CONTRACT.mode, 0o755)
        self.assertEqual(HELPER.CONTRACT.pre_size, 59516)
        self.assertEqual(HELPER.CONTRACT.post_size, 58398)
        self.assertEqual(len(HELPER.OLD_FRAGMENT), 1268)
        self.assertEqual(len(HELPER.NEW_FRAGMENT), 150)
        self.assertEqual(
            hashlib.sha256(HELPER.OLD_FRAGMENT).hexdigest(),
            "47191891df64e7aff903e128ad48e4668fdf5499632daa83eb85798a5b18d776",
        )
        self.assertEqual(
            hashlib.sha256(HELPER.NEW_FRAGMENT).hexdigest(),
            "73c20aeb27da94e5002f672fc3edc34ea3fd41e5436e3738d4e2be026a651a97",
        )
        self.assertEqual(set(HELPER.CONTRACT.peer_sources), {"oos15-global", "oos16"})

        manifest = ET.parse(ROOT / "manifests" / "lockfiles" / "oos15-cn.xml")
        projects = [
            project
            for project in manifest.getroot().findall("project")
            if project.get("name")
            == "android_kernel_modules_and_devicetree_oneplus_sm8750"
        ]
        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0].get("path"), "./")
        self.assertEqual(projects[0].get("revision"), HELPER.CONTRACT.source_commit)


class Oos15CnHmbirdOverlayWiringTests(unittest.TestCase):
    def test_operation_is_cn_hmbird_only_and_ordered_before_other_compatibility(self) -> None:
        series_id, operations = _load_series(ROOT / "patches" / "series" / "wild.yml")
        self.assertEqual(series_id, "wild")
        operation_by_id = {operation["id"]: operation for operation in operations}
        operation = operation_by_id["hmbird-sched-assist-oos15-cn"]
        self.assertEqual(operation["type"], "exec")
        self.assertEqual(operation["cwd"], ".")
        self.assertEqual(operation["bases"], ["oos15-cn"])
        self.assertEqual(operation["feature"], "oneplus.hmbird_fengchi_scx")
        self.assertEqual(
            operation["argv"],
            [
                "python3",
                "{repo_root}/scripts/integrate-oos15-cn-hmbird-overlay.py",
                "--source-dir",
                "{source_dir}",
                "--base",
                "{base}",
            ],
        )
        self.assertEqual(
            operation["expected_outputs"],
            [".op13/oos15-cn-hmbird-overlay.json"],
        )
        ids = [item["id"] for item in operations]
        self.assertLess(
            ids.index("hmbird-fengchi-oneplus13-vendor"),
            ids.index(operation["id"]),
        )
        self.assertLess(
            ids.index(operation["id"]),
            ids.index("hmbird-cpufreq-api-oos15-cn"),
        )
        self.assertLess(
            ids.index(operation["id"]),
            ids.index("hmbird-device-tree-overwriter"),
        )

    def test_selection_is_limited_to_full_and_wild_cn_builds(self) -> None:
        _, _, profiles, features = discover_configs(ROOT)
        _, operations = _load_series(ROOT / "patches" / "series" / "wild.yml")
        operation = next(
            item for item in operations if item["id"] == "hmbird-sched-assist-oos15-cn"
        )
        enabled = {
            (feature.id, base)
            for feature in features.values()
            for base in profiles
            if _operation_enabled(operation, feature, base, "kernelsu-next")
        }
        self.assertEqual(enabled, {("full", "oos15-cn"), ("wild", "oos15-cn")})

    def test_build_and_rehearsal_diagnostics_retain_the_source_stamp(self) -> None:
        build = (ROOT / ".github" / "workflows" / "build.yml").read_text(
            encoding="utf-8"
        )
        validate = (ROOT / ".github" / "workflows" / "validate.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("out/source/.op13", build)
        self.assertIn(
            "cp -a out/source/.op13 out/debug/source-context",
            validate,
        )


if __name__ == "__main__":
    unittest.main()
