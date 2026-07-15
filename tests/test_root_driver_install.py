from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("install_root_driver", ROOT / "scripts" / "install-root-driver.py")
assert SPEC is not None and SPEC.loader is not None
HELPER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HELPER)


class RootDriverInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name).resolve()
        self.root_dir = self.workspace / "kernel_platform" / "KernelSU-Next"
        kernel = self.root_dir / "kernel"
        kernel.mkdir(parents=True)
        (kernel / "Kconfig").write_text("config KSU\n", encoding="utf-8")
        (kernel / "Kbuild").write_text("obj-y += core/\n", encoding="utf-8")
        (kernel / "core").mkdir()
        (kernel / "core" / "main.c").write_text("int ksu;\n", encoding="utf-8")
        self.destination = self.workspace / "kernel_platform" / "common" / "drivers" / "kernelsu"
        files = HELPER._source_files(kernel)
        expected = {"file_count": len(files), "tree_sha256": HELPER.tree_digest(kernel, files)}
        self.tree_patch = mock.patch.dict(
            HELPER.EXPECTED_TREES,
            {"kernelsu": expected, "kernelsu-next": expected},
        )
        self.tree_patch.start()

    def tearDown(self) -> None:
        self.tree_patch.stop()
        self.temporary.cleanup()

    def test_install_records_variant_and_reproducible_tree_digest(self) -> None:
        files = HELPER._source_files(self.root_dir / "kernel")
        expected = HELPER.tree_digest(self.root_dir / "kernel", files)
        document = HELPER.install(self.workspace, self.root_dir, self.destination, "kernelsu-next")
        self.assertEqual(document["tree_sha256"], expected)
        self.assertEqual(document["variant"], "kernelsu-next")
        stamp = json.loads((self.destination / HELPER.STAMP_NAME).read_text(encoding="utf-8"))
        self.assertEqual(stamp["tree_sha256"], expected)
        self.assertTrue((self.destination / "core" / "main.c").is_file())

    def test_existing_destination_is_rejected(self) -> None:
        self.destination.mkdir(parents=True)
        with self.assertRaisesRegex(HELPER.InstallError, "already exists"):
            HELPER.install(self.workspace, self.root_dir, self.destination, "kernelsu")

    def test_destination_escape_is_rejected(self) -> None:
        escaped = self.workspace.parent / "outside" / "kernelsu"
        with self.assertRaisesRegex(HELPER.InstallError, "escapes"):
            HELPER.install(self.workspace, self.root_dir, escaped, "kernelsu")

    def test_symlink_source_is_rejected(self) -> None:
        link = self.root_dir / "kernel" / "unsafe"
        try:
            link.symlink_to(self.workspace / "outside")
        except OSError:
            self.skipTest("symlink creation is unavailable")
        with self.assertRaisesRegex(HELPER.InstallError, "symlink"):
            HELPER.install(self.workspace, self.root_dir, self.destination, "kernelsu-next")


if __name__ == "__main__":
    unittest.main()
