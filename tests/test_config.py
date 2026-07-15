from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lib.config import discover_configs, load_dependency_lock, load_json_yaml, validate_repository
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
        _, _, _, features = discover_configs(self.root)
        self.assertEqual(features["test"].required_symbols["CONFIG_MT76x0U"], "m")

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


if __name__ == "__main__":
    unittest.main()
