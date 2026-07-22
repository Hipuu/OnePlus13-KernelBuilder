from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RootWorkflowInputTests(unittest.TestCase):
    def test_build_workflow_records_optional_commit_assertions_before_sync(self) -> None:
        text = (ROOT / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8")
        self.assertEqual(text.count("      kernelsu_commit:\n"), 2)
        self.assertEqual(text.count("      susfs_commit:\n"), 2)
        self.assertIn("REQUESTED_KERNELSU_COMMIT: ${{ inputs.kernelsu_commit }}", text)
        self.assertIn("REQUESTED_SUSFS_COMMIT: ${{ inputs.susfs_commit }}", text)
        resolver = text.index("python3 scripts/op13.py resolve-root-lock")
        source_sync = text.index("bash scripts/run-hosted-source-sync.sh")
        self.assertLess(resolver, source_sync)
        self.assertIn('> "$DEBUG_DIR/root-selection.json"', text)

    def test_release_workflow_validates_and_forwards_commit_assertions(self) -> None:
        text = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        self.assertEqual(text.count("      kernelsu_commit:\n"), 1)
        self.assertEqual(text.count("      susfs_commit:\n"), 1)
        validator = text.split("  validate-release-inputs:\n", 1)[1].split(
            "\n  module-kernel-prerequisite:\n", 1
        )[0]
        self.assertIn("REQUESTED_KERNELSU_COMMIT: ${{ inputs.kernelsu_commit }}", validator)
        self.assertIn("REQUESTED_SUSFS_COMMIT: ${{ inputs.susfs_commit }}", validator)
        self.assertIn("python3 scripts/op13.py resolve-root-lock", validator)
        self.assertIn('--kernelsu-commit "$REQUESTED_KERNELSU_COMMIT"', validator)
        self.assertIn('--susfs-commit "$REQUESTED_SUSFS_COMMIT"', validator)

        prerequisite = text.split("  module-kernel-prerequisite:\n", 1)[1].split(
            "\n  rebuild:\n", 1
        )[0]
        rebuild = text.split("  rebuild:\n", 1)[1].split("\n  publish:\n", 1)[0]
        for caller in (prerequisite, rebuild):
            self.assertIn(
                "      kernelsu_commit: ${{ inputs.kernelsu_commit }}\n", caller
            )
            self.assertIn("      susfs_commit: ${{ inputs.susfs_commit }}\n", caller)


if __name__ == "__main__":
    unittest.main()
