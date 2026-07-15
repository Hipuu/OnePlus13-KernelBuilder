from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "build.yml"


class HostedCompileCacheContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = WORKFLOW.read_text(encoding="utf-8")

    def test_bazel_cache_is_scoped_to_nonclean_kernel_builds(self) -> None:
        condition = "inputs.cache && !inputs.clean && inputs.target != 'modules'"
        restore = self.text.index("uses: actions/cache/restore@")
        path_guard = self.text.index("- name: Validate compile cache filesystem path")
        build = self.text.index("bash scripts/build-kernel.sh")
        save = self.text.index("uses: actions/cache/save@")
        modules = self.text.index("bash scripts/build-modules.sh")
        source_release = self.text.index("- name: Release source checkout")

        self.assertEqual(self.text.count("uses: actions/cache/restore@"), 1)
        self.assertEqual(self.text.count("uses: actions/cache/save@"), 1)
        self.assertGreaterEqual(self.text.count(condition), 4)
        self.assertLess(self.text.index("bash scripts/sync-sources.sh"), path_guard)
        self.assertLess(path_guard, restore)
        self.assertLess(restore, build)
        self.assertLess(build, save)
        self.assertLess(save, modules)
        self.assertLess(save, source_release)

    def test_key_has_all_selectors_and_one_canonical_input_hash(self) -> None:
        canonical_hash = (
            "hashFiles('manifests/lockfiles/**', 'dependencies/lock.yml', "
            "'configs/**', 'schemas/**', 'patches/**', 'scripts/**')"
        )
        prefix = (
            'restore_prefix="op13-bazel-v1-${RUNNER_OS_NAME}-${RUNNER_ARCH_NAME}-'
            '${BASE}-${ROOT_VARIANT}-${PROFILE}-${OPTIMIZATION}-${LTO_MODE}-"'
        )

        self.assertEqual(self.text.count(canonical_hash), 1)
        self.assertIn(prefix, self.text)
        self.assertIn('cache_key="${restore_prefix}${COMPILE_CACHE_INPUT_HASH}"', self.text)
        self.assertIn("${{ steps.compile-cache-identity.outputs.restore-prefix }}", self.text)
        self.assertNotIn("restore-keys: |\n            op13-bazel-v1-", self.text)

    def test_size_limit_and_debug_evidence_are_explicit(self) -> None:
        self.assertIn(
            "COMPILE_CACHE_MAX_BYTES: ${{ vars.OP13_COMPILE_CACHE_MAX_BYTES || "
            "'7516192768' }}",
            self.text,
        )
        self.assertIn("must be a positive decimal byte count", self.text)
        self.assertIn("reason=oversize", self.text)
        self.assertIn("steps.compile-cache-size.outputs.save == 'true'", self.text)
        for field in (
            "compile_cache_key",
            "compile_cache_restore_primary_key",
            "compile_cache_restore_matched_key",
            "compile_cache_pre_bytes",
            "compile_cache_post_bytes",
            "compile_cache_limit_bytes",
            "compile_cache_save_outcome",
            "compile_cache_save_reason",
            "compile_cache_save_key",
        ):
            with self.subTest(field=field):
                self.assertIn(field, self.text)

    def test_cache_path_comes_from_device_config_and_not_module_work(self) -> None:
        self.assertIn(".official_build.cache_dir", self.text)
        self.assertIn('cache_path="$SOURCE_DIR/kernel_platform/$cache_dir"', self.text)
        self.assertIn("path: ${{ steps.compile-cache-identity.outputs.path }}", self.text)
        self.assertNotIn("out/build/modules/work", self.text)
        self.assertNotIn("cache_max_gib", self.text)

    def test_schema_and_workflow_reject_dot_traversal_segments(self) -> None:
        schema = json.loads((ROOT / "schemas" / "device.schema.json").read_text(encoding="utf-8"))
        pattern = schema["properties"]["official_build"]["properties"]["cache_dir"]["pattern"]
        for invalid in (".", "..", "cache/./nested", "cache/../../.git"):
            with self.subTest(invalid=invalid):
                self.assertIsNone(re.fullmatch(pattern, invalid))
        self.assertIsNotNone(re.fullmatch(pattern, "bazel-cache"))
        self.assertIn('if [[ "$cache_part" == "." || "$cache_part" == ".." ]]', self.text)
        self.assertIn('if [[ -L "$current" ]]', self.text)


if __name__ == "__main__":
    unittest.main()
