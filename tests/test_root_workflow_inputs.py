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
        source_sync = text.index("bash scripts/sync-sources.sh")
        self.assertLess(resolver, source_sync)
        self.assertIn('> "$DEBUG_DIR/root-selection.json"', text)


if __name__ == "__main__":
    unittest.main()
