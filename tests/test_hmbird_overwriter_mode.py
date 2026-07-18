from __future__ import annotations

import dataclasses
import importlib.util
import json
import os
import stat
import subprocess
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
    "integrate_hmbird_overwriter_mode",
    ROOT / "scripts" / "integrate-hmbird-overwriter-mode.py",
)
assert SPEC is not None and SPEC.loader is not None
HELPER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = HELPER
SPEC.loader.exec_module(HELPER)


def _git(checkout: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(checkout), *arguments],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


class HmbirdOverwriterModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.patch_payload = (
            b"diff --git a/drivers/of/overwriter/overwrite_configs/convert_configs.sh "
            b"b/drivers/of/overwriter/overwrite_configs/convert_configs.sh\n"
            b"new file mode 100644\n"
        )
        self.target_payload = b"#!/bin/sh\nset -eu\nprintf '%s\\n' fixture\n"
        self.wild = self.root / "wild"
        self.wild.mkdir()
        _git(self.wild, "init", "-q")
        _git(self.wild, "config", "user.name", "fixture")
        _git(self.wild, "config", "user.email", "fixture@example.invalid")
        _git(self.wild, "config", "core.autocrlf", "false")
        patch = self.wild / HELPER.CONTRACT.patch_path
        patch.parent.mkdir(parents=True)
        patch.write_bytes(self.patch_payload)
        _git(self.wild, "add", HELPER.CONTRACT.patch_path)
        _git(self.wild, "commit", "-q", "-m", "fixture")
        head = _git(self.wild, "rev-parse", "HEAD")

        self.source, self.targets = self._make_source("source")
        self.pre_mode = stat.S_IMODE(self.targets["common"].stat().st_mode)
        post_mode = 0o755 if self.pre_mode != 0o755 else 0o700
        self.contract = dataclasses.replace(
            HELPER.CONTRACT,
            wild_commit=head,
            patch_blob=HELPER.git_blob_oid(self.patch_payload),
            patch_sha256=HELPER.sha256_bytes(self.patch_payload),
            patch_size=len(self.patch_payload),
            target_blob=HELPER.git_blob_oid(self.target_payload),
            target_sha256=HELPER.sha256_bytes(self.target_payload),
            target_size=len(self.target_payload),
            pre_mode=self.pre_mode,
            post_mode=post_mode,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _make_source(self, name: str) -> tuple[Path, dict[str, Path]]:
        source = self.root / name
        (source / HELPER.STAMP_RELATIVE.parent).mkdir(parents=True)
        targets: dict[str, Path] = {}
        for tree, relative in HELPER.TREE_RELATIVES.items():
            target = source / relative / HELPER.TARGET_RELATIVE
            target.parent.mkdir(parents=True)
            target.write_bytes(self.target_payload)
            target.chmod(0o644)
            targets[tree] = target
        return source, targets

    @unittest.skipIf(os.name == "nt", "Windows does not expose POSIX execute bits")
    def test_all_three_bases_repair_both_trees_without_changing_bytes(self) -> None:
        for index, base in enumerate(self.contract.bases):
            with self.subTest(base=base):
                source, targets = self._make_source(f"source-{index}")
                document = HELPER.integrate(
                    source,
                    self.wild,
                    base,
                    contract=self.contract,
                )

                self.assertEqual(document["base"], base)
                self.assertEqual(document["inputs"]["wild_commit"], self.contract.wild_commit)
                self.assertEqual(document["inputs"]["patch"]["blob"], self.contract.patch_blob)
                self.assertEqual(set(document["targets"]), {"common", "msm-kernel"})
                for tree, target in targets.items():
                    self.assertEqual(target.read_bytes(), self.target_payload)
                    self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)
                    self.assertEqual(document["targets"][tree]["pre_mode"], "0644")
                    self.assertEqual(document["targets"][tree]["post_mode"], "0755")
                stamp = source / HELPER.STAMP_RELATIVE
                self.assertEqual(json.loads(stamp.read_text(encoding="utf-8")), document)

    def test_one_bad_tree_is_rejected_before_either_tree_changes(self) -> None:
        self.targets["msm-kernel"].write_bytes(b"X" + self.target_payload[1:])
        before = {
            tree: (target.read_bytes(), stat.S_IMODE(target.stat().st_mode))
            for tree, target in self.targets.items()
        }

        with self.assertRaisesRegex(HELPER.IntegrationError, "preimage changed"):
            HELPER.integrate(
                self.source,
                self.wild,
                "oos16",
                contract=self.contract,
            )

        for tree, target in self.targets.items():
            self.assertEqual(
                (target.read_bytes(), stat.S_IMODE(target.stat().st_mode)),
                before[tree],
            )
        self.assertFalse((self.source / HELPER.STAMP_RELATIVE).exists())

    def test_base_commit_patch_and_checkout_cleanliness_are_fail_closed(self) -> None:
        with self.assertRaisesRegex(HELPER.IntegrationError, "not valid"):
            HELPER.integrate(
                self.source,
                self.wild,
                "android15-6.6",
                contract=self.contract,
            )

        wrong_commit = dataclasses.replace(self.contract, wild_commit="0" * 40)
        with self.assertRaisesRegex(HELPER.IntegrationError, "commit changed"):
            HELPER.integrate(
                self.source,
                self.wild,
                "oos15-cn",
                contract=wrong_commit,
            )

        wrong_patch = dataclasses.replace(self.contract, patch_sha256="0" * 64)
        with self.assertRaisesRegex(HELPER.IntegrationError, "patch changed"):
            HELPER.integrate(
                self.source,
                self.wild,
                "oos15-global",
                contract=wrong_patch,
            )

        (self.wild / "untracked").write_text("dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(HELPER.IntegrationError, "not byte-clean"):
            HELPER.integrate(
                self.source,
                self.wild,
                "oos16",
                contract=self.contract,
            )

    def test_wrong_mode_missing_target_and_existing_stamp_are_rejected(self) -> None:
        wrong_pre_mode = self.pre_mode ^ stat.S_IRUSR
        wrong_mode = dataclasses.replace(
            self.contract,
            pre_mode=wrong_pre_mode,
            post_mode=0o755 if wrong_pre_mode != 0o755 else 0o700,
        )
        with self.assertRaisesRegex(HELPER.IntegrationError, "mode changed"):
            HELPER.integrate(
                self.source,
                self.wild,
                "oos15-cn",
                contract=wrong_mode,
            )

        missing_source, missing_targets = self._make_source("missing-source")
        missing_targets["msm-kernel"].unlink()
        with self.assertRaisesRegex(HELPER.IntegrationError, "is missing"):
            HELPER.integrate(
                missing_source,
                self.wild,
                "oos15-global",
                contract=self.contract,
            )

        duplicate_source, _ = self._make_source("duplicate-source")
        (duplicate_source / HELPER.STAMP_RELATIVE).write_text(
            "{}\n", encoding="utf-8", newline="\n"
        )
        with self.assertRaisesRegex(HELPER.IntegrationError, "already exists"):
            HELPER.integrate(
                duplicate_source,
                self.wild,
                "oos16",
                contract=self.contract,
            )

    def test_symlink_target_is_rejected(self) -> None:
        source, targets = self._make_source("symlink-source")
        outside = self.root / "outside-converter"
        outside.write_bytes(self.target_payload)
        targets["msm-kernel"].unlink()
        try:
            targets["msm-kernel"].symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")

        with self.assertRaisesRegex(HELPER.IntegrationError, "symlink or reparse"):
            HELPER.integrate(
                source,
                self.wild,
                "oos15-cn",
                contract=self.contract,
            )

    @unittest.skipIf(os.name == "nt", "Windows does not expose POSIX execute bits")
    def test_second_chmod_and_stamp_failures_restore_both_modes(self) -> None:
        original_set_mode = HELPER._set_mode
        calls = 0

        def fail_second_mode(path: Path, mode: int) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("forced second chmod failure")
            original_set_mode(path, mode)

        with mock.patch.object(HELPER, "_set_mode", side_effect=fail_second_mode):
            with self.assertRaisesRegex(OSError, "forced second chmod failure"):
                HELPER.integrate(
                    self.source,
                    self.wild,
                    "oos15-cn",
                    contract=self.contract,
                )
        for target in self.targets.values():
            self.assertEqual(target.read_bytes(), self.target_payload)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)
        self.assertFalse((self.source / HELPER.STAMP_RELATIVE).exists())

        stamp_source, stamp_targets = self._make_source("stamp-source")
        with mock.patch.object(
            HELPER,
            "_atomic_json",
            side_effect=RuntimeError("forced stamp failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "forced stamp failure"):
                HELPER.integrate(
                    stamp_source,
                    self.wild,
                    "oos15-global",
                    contract=self.contract,
                )
        for target in stamp_targets.values():
            self.assertEqual(target.read_bytes(), self.target_payload)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)
        self.assertFalse((stamp_source / HELPER.STAMP_RELATIVE).exists())

    @unittest.skipIf(os.name == "nt", "Windows does not expose POSIX execute bits")
    def test_already_executable_target_is_rejected(self) -> None:
        self.targets["common"].chmod(self.contract.post_mode)
        with self.assertRaisesRegex(HELPER.IntegrationError, "already executable"):
            HELPER.integrate(
                self.source,
                self.wild,
                "oos16",
                contract=self.contract,
            )

    def test_real_contract_is_cross_locked_to_wild_dependency(self) -> None:
        lock = json.loads((ROOT / "dependencies" / "lock.yml").read_text())
        dependency = lock["dependencies"]["wild_kernel_patches"]
        self.assertEqual(HELPER.CONTRACT.wild_commit, dependency["commit"])
        self.assertEqual(
            HELPER.CONTRACT.bases,
            ("oos15-cn", "oos15-global", "oos16"),
        )
        self.assertEqual(HELPER.CONTRACT.patch_path, "oneplus/hmbird/overwriter.patch")
        self.assertEqual(HELPER.CONTRACT.patch_size, 39515)
        self.assertEqual(
            HELPER.CONTRACT.patch_blob,
            "7a573dbe50eecaa2ca89b325dce3b274a2d4bd91",
        )
        self.assertEqual(
            HELPER.CONTRACT.patch_sha256,
            "f9963385662591cab6c7ca159628c83a50ba7cf834a726e7749880046a6c8572",
        )
        self.assertEqual(HELPER.CONTRACT.target_size, 3528)
        self.assertEqual(
            HELPER.CONTRACT.target_blob,
            "b05dabb860650cc721702f63fc22f093de621958",
        )
        self.assertEqual(
            HELPER.CONTRACT.target_sha256,
            "cf3077459d8b1023912a3eac9996d9133503b192d080de186a59663d4b050418",
        )
        self.assertEqual(HELPER.CONTRACT.pre_mode, 0o644)
        self.assertEqual(HELPER.CONTRACT.post_mode, 0o755)


class HmbirdOverwriterModeWiringTests(unittest.TestCase):
    def test_operation_is_single_all_base_gate_between_overwriter_patches(self) -> None:
        series_id, operations = _load_series(ROOT / "patches" / "series" / "wild.yml")
        self.assertEqual(series_id, "wild")
        operation = next(
            item for item in operations if item["id"] == "hmbird-overwriter-executable"
        )
        self.assertEqual(operation["type"], "exec")
        self.assertEqual(operation["dependency"], "wild_kernel_patches")
        self.assertEqual(operation["cwd"], ".")
        self.assertNotIn("bases", operation)
        self.assertNotIn("kernel_trees", operation)
        self.assertEqual(operation["feature"], "oneplus.hmbird_fengchi_scx")
        self.assertEqual(
            operation["argv"],
            [
                "python3",
                "{repo_root}/scripts/integrate-hmbird-overwriter-mode.py",
                "--source-dir",
                "{source_dir}",
                "--wild-dir",
                "{dependency_dir:wild_kernel_patches}",
                "--base",
                "{base}",
            ],
        )
        self.assertEqual(
            operation["expected_outputs"],
            [".op13/hmbird-overwriter-mode.json"],
        )
        ids = [item["id"] for item in operations]
        self.assertEqual(
            ids.index(operation["id"]),
            ids.index("hmbird-device-tree-overwriter") + 1,
        )
        self.assertEqual(
            ids.index("hmbird-ogki-device-tree-config"),
            ids.index(operation["id"]) + 1,
        )

    def test_selection_is_full_and_wild_on_all_three_bases(self) -> None:
        _, _, profiles, features = discover_configs(ROOT)
        _, operations = _load_series(ROOT / "patches" / "series" / "wild.yml")
        operation = next(
            item for item in operations if item["id"] == "hmbird-overwriter-executable"
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
                for base in ("oos15-cn", "oos15-global", "oos16")
            },
        )


if __name__ == "__main__":
    unittest.main()
