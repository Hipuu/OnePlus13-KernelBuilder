from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lib.artifacts import _portable_provenance_document
from lib.errors import BuildToolError


class PackagedPathPortabilityTests(unittest.TestCase):
    @staticmethod
    def _document(root: Path) -> dict[str, object]:
        return {
            "artifacts": [
                {"path": str(root / "out" / "dist" / "kernel-Image")}
            ],
            "patches": [
                {
                    "target": str(root / "out" / "source" / "kernel.c"),
                    "argv": [
                        "python3",
                        str(root / "scripts" / "integrate.py"),
                    ],
                }
            ],
            "configuration": {
                "config_path": str(root / "out" / "build" / ".config")
            },
            "kernel": {
                "kernel_kit": str(root / "out" / "build" / "kernel-kit")
            },
            "modules": {
                "staging": str(root / "out" / "build" / "modules" / "staging"),
                "relative_member": "lib/modules/fixture.ko",
            },
            "future_record_field": str(root / "out" / "future" / "evidence.json"),
            "source_url": "https://github.com/OnePlusOSS/kernel_manifest.git",
        }

    def test_equivalent_builds_under_different_roots_have_equal_paths(self) -> None:
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            first_root = Path(first) / "workspace"
            second_root = Path(second) / "workspace"
            first_root.mkdir()
            second_root.mkdir()
            first_document = _portable_provenance_document(
                self._document(first_root),
                first_root,
            )
            second_document = _portable_provenance_document(
                self._document(second_root),
                second_root,
            )
        self.assertEqual(first_document, second_document)
        self.assertEqual(
            first_document["artifacts"][0]["path"],
            "out/dist/kernel-Image",
        )
        self.assertEqual(
            first_document["patches"][0]["argv"][1],
            "scripts/integrate.py",
        )
        self.assertEqual(
            first_document["future_record_field"],
            "out/future/evidence.json",
        )
        self.assertEqual(
            first_document["source_url"],
            "https://github.com/OnePlusOSS/kernel_manifest.git",
        )

    def test_absolute_path_outside_workspace_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            root.mkdir()
            outside = Path(temporary) / "outside" / "artifact"
            with self.assertRaisesRegex(BuildToolError, "escapes the workspace"):
                _portable_provenance_document({"path": str(outside)}, root)


if __name__ == "__main__":
    unittest.main()
