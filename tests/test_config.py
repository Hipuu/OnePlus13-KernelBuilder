from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lib.config import (
    discover_configs,
    load_dependency_lock,
    load_json_yaml,
    resolve_root_selection,
    validate_repository,
)
from lib.errors import BuildToolError
from tests.support import make_repository


class ConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = make_repository(Path(self.temporary.name))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_fixture_repository_is_valid_and_preserves_mixed_case_symbol(self) -> None:
        summary = validate_repository(self.root)
        self.assertEqual(summary["target"], "sun")
        device, _, _, features = discover_configs(self.root)
        self.assertEqual(device.official_cache_dir, "bazel-cache")
        self.assertEqual(features["test"].required_symbols["CONFIG_MT76x0U"], "m")
        self.assertEqual(features["test"].kconfig_fragments[0].kernel_trees, ("common",))

    def test_kconfig_fragment_can_target_both_locked_kernel_trees(self) -> None:
        path = self.root / "configs" / "features" / "test.yml"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["kconfig_fragments"][0]["kernel_trees"] = ["common", "msm-kernel"]
        path.write_text(json.dumps(data), encoding="utf-8")
        _, _, _, features = discover_configs(self.root)
        self.assertEqual(
            features["test"].kconfig_fragments[0].kernel_trees,
            ("common", "msm-kernel"),
        )

    def test_kconfig_fragment_rejects_invalid_kernel_tree_sets(self) -> None:
        path = self.root / "configs" / "features" / "test.yml"
        original = json.loads(path.read_text(encoding="utf-8"))
        for invalid in ([], ["common", "common"], ["vendor"]):
            with self.subTest(invalid=invalid):
                data = json.loads(json.dumps(original))
                data["kconfig_fragments"][0]["kernel_trees"] = invalid
                path.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaisesRegex(BuildToolError, "kernel tree"):
                    discover_configs(self.root)

    def test_official_build_cache_must_be_kernel_platform_relative(self) -> None:
        path = self.root / "configs" / "devices" / "oneplus13.yml"
        for invalid in (
            "../bazel-cache",
            "/tmp/bazel-cache",
            ".",
            r"C:\\tmp",
            "cache//nested",
            "cache/./nested",
            "cache/../../.git",
        ):
            with self.subTest(invalid=invalid):
                data = json.loads(path.read_text(encoding="utf-8"))
                data["official_build"]["cache_dir"] = invalid
                path.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaisesRegex(BuildToolError, "cache.*kernel-platform-relative"):
                    discover_configs(self.root)
                data["official_build"]["cache_dir"] = "bazel-cache"
                path.write_text(json.dumps(data), encoding="utf-8")

    def test_git_dependency_requires_full_commit(self) -> None:
        path = self.root / "dependencies" / "lock.yml"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["dependencies"]["rtw88"].pop("commit")
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(BuildToolError, "full commit SHA"):
            load_dependency_lock(path)

    def test_git_dependency_rejects_moving_branch_ref(self) -> None:
        path = self.root / "dependencies" / "lock.yml"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["dependencies"]["rtw88"]["ref"] = "refs/heads/master"
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(BuildToolError, "mutable branch ref"):
            load_dependency_lock(path)

    def test_download_requires_sha256(self) -> None:
        path = self.root / "dependencies" / "lock.yml"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["dependencies"]["repo_launcher"].pop("sha256")
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(BuildToolError, "needs sha256"):
            load_dependency_lock(path)

    def test_platform_cross_mix_is_rejected(self) -> None:
        path = self.root / "configs" / "profiles" / "oos16.yml"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["kmi"] = "android16-6.12"
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(BuildToolError, "platform mismatch"):
            discover_configs(self.root)

    def test_embedded_github_token_is_rejected_before_parse(self) -> None:
        path = self.root / "bad.yml"
        path.write_text('{"token":"ghp_' + "A" * 36 + '"}', encoding="utf-8")
        with self.assertRaisesRegex(BuildToolError, "credential"):
            load_json_yaml(path)

    def test_non_json_yaml_is_rejected_with_clear_contract(self) -> None:
        path = self.root / "bad.yml"
        path.write_text("schema_version: 1\n", encoding="utf-8")
        with self.assertRaisesRegex(BuildToolError, "JSON-compatible YAML"):
            load_json_yaml(path)

    def _load_root_lock(self):
        path = self.root / "dependencies" / "lock.yml"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["dependencies"]["susfs"] = {
            "kind": "git",
            "url": "https://example.com/susfs.git",
            "commit": "8" * 40,
            "required_for": ["susfs"],
        }
        data["dependencies"]["wild_kernel_patches"] = {
            "kind": "git",
            "url": "https://example.com/wild.git",
            "commit": "9" * 40,
            "required_for": ["wild"],
        }
        path.write_text(json.dumps(data), encoding="utf-8")
        return load_dependency_lock(path)

    def test_root_commit_assertions_resolve_only_the_audited_pair(self) -> None:
        lock = self._load_root_lock()
        classic = resolve_root_selection(lock, "kernelsu", "4" * 40, "8" * 40)
        self.assertEqual(classic["root"]["dependency"], "kernelsu")
        self.assertEqual(classic["root"]["commit"], "4" * 40)
        self.assertNotIn("compatibility_patches", classic)
        next_selection = resolve_root_selection(lock, "kernelsu-next")
        self.assertEqual(next_selection["root"]["commit"], "5" * 40)
        self.assertEqual(next_selection["susfs"]["commit"], "8" * 40)
        self.assertEqual(next_selection["compatibility_patches"]["commit"], "9" * 40)

    def test_root_commit_assertions_reject_unreviewed_or_noncanonical_shas(self) -> None:
        lock = self._load_root_lock()
        with self.assertRaisesRegex(BuildToolError, "not the audited lock"):
            resolve_root_selection(lock, "kernelsu-next", "a" * 40, "8" * 40)
        with self.assertRaisesRegex(BuildToolError, "not the audited lock"):
            resolve_root_selection(lock, "kernelsu-next", "4" * 40, "8" * 40)
        with self.assertRaisesRegex(BuildToolError, "not the audited lock"):
            resolve_root_selection(lock, "kernelsu", "5" * 40, "8" * 40)
        with self.assertRaisesRegex(BuildToolError, "not the audited lock"):
            resolve_root_selection(lock, "kernelsu-next", "5" * 40, "7" * 40)
        with self.assertRaisesRegex(BuildToolError, "lowercase 40-character SHA"):
            resolve_root_selection(lock, "kernelsu-next", "A" * 40, "8" * 40)
        with self.assertRaisesRegex(BuildToolError, "lowercase 40-character SHA"):
            resolve_root_selection(lock, "kernelsu-next", "5" * 40, "A" * 40)
        with self.assertRaisesRegex(BuildToolError, "lowercase 40-character SHA"):
            resolve_root_selection(lock, "kernelsu-next", "short", "8" * 40)

    def test_root_none_rejects_unused_commit_assertions(self) -> None:
        lock = self._load_root_lock()
        selection = resolve_root_selection(lock, "none")
        self.assertIsNone(selection["root"])
        self.assertIsNone(selection["susfs"])
        with self.assertRaisesRegex(BuildToolError, "root=none"):
            resolve_root_selection(lock, "none", "4" * 40, "")


if __name__ == "__main__":
    unittest.main()
