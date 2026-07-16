from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / "patches" / "oneplus13" / "0001-module-overlay-source-only.patch"
SCRIPT = "kernel/module/module_overlay/convert_overlay.sh"


class ModuleOverlayExecutableTests(unittest.TestCase):
    def test_convert_overlay_patch_records_executable_git_mode(self) -> None:
        payload = PATCH.read_text(encoding="utf-8")
        marker = f"diff --git a/{SCRIPT} b/{SCRIPT}\n"
        start = payload.index(marker)
        end = payload.find("\ndiff --git ", start + len(marker))
        script_diff = payload[start:] if end == -1 else payload[start:end] + "\n"

        self.assertIn(f"create mode 100755 {SCRIPT}", payload)

        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            patch_path = repository / "convert-overlay.patch"
            patch_path.write_text(script_diff, encoding="utf-8", newline="\n")
            subprocess.run(
                ["git", "init", "--quiet"],
                cwd=repository,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "-c", "core.autocrlf=false", "apply", "--cached", str(patch_path)],
                cwd=repository,
                check=True,
                capture_output=True,
                text=True,
            )
            staged = subprocess.run(
                ["git", "ls-files", "--stage", "--", SCRIPT],
                cwd=repository,
                check=True,
                capture_output=True,
                text=True,
            ).stdout

        self.assertRegex(staged, rf"^100755 [0-9a-f]+ 0\t{SCRIPT}\n$")


if __name__ == "__main__":
    unittest.main()
