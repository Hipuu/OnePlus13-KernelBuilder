from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class HostedRunnerDiskContractTests(unittest.TestCase):
    def test_source_and_build_jobs_reclaim_full_unused_tool_cache(self) -> None:
        for relative in (
            ".github/workflows/validate.yml",
            ".github/workflows/build.yml",
        ):
            with self.subTest(workflow=relative):
                text = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn("/opt/hostedtoolcache\n", text)
                self.assertNotIn("/opt/hostedtoolcache/CodeQL", text)
                self.assertIn("minimum_available=$((100 * 1024 * 1024 * 1024))", text)
                self.assertIn("less than 100 GiB is available", text)
                self.assertIn("resolved=$(realpath -e -- \"$candidate\")", text)
                self.assertIn("sudo rm -rf --one-file-system -- \"$resolved\"", text)


if __name__ == "__main__":
    unittest.main()
