from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("integrate_ksun_susfs", ROOT / "scripts" / "integrate-ksun-susfs.py")
assert SPEC is not None and SPEC.loader is not None
HELPER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HELPER)


class KsunIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.ksun = self.root / "ksun"
        self.susfs = self.root / "susfs"
        self.wild = self.root / "wild"
        (self.ksun / "kernel").mkdir(parents=True)
        (self.ksun / "kernel" / "Kconfig").write_text("config KSU_SUSFS\n\tbool\n", encoding="utf-8")
        susfs_patch = self.susfs / HELPER.SUSFS_PATCH
        susfs_patch.parent.mkdir(parents=True)
        susfs_patch.write_text("base\n", encoding="utf-8")
        fix_root = self.wild / HELPER.WILD_FIX_DIR
        fix_root.mkdir(parents=True)
        for name in HELPER.FIX_PATCHES:
            (fix_root / name).write_text(name + "\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _make_rejects(self, rejects) -> None:
        for relative in rejects:
            path = self.ksun / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("reject\n", encoding="utf-8")

    def test_exact_reject_fingerprint_and_fix_order_produce_stamp(self) -> None:
        calls: list[str] = []

        def fake_patch(tree: Path, patch_file: Path):
            calls.append(patch_file.name)
            if patch_file.name == HELPER.SUSFS_PATCH.name:
                self._make_rejects(HELPER.EXPECTED_REJECTS)
                return 1, "expected partial application"
            return 0, "applied"

        with mock.patch.object(HELPER, "_gnu_patch_version", return_value="GNU patch 2.7.6"), mock.patch.object(
            HELPER, "_run_patch", side_effect=fake_patch
        ):
            document = HELPER.integrate(self.ksun, self.susfs, self.wild)
        self.assertEqual(calls, [HELPER.SUSFS_PATCH.name, *HELPER.FIX_PATCHES])
        self.assertEqual(document["base_patch"]["rejects"], sorted(HELPER.EXPECTED_REJECTS))
        self.assertTrue((self.ksun / ".op13-susfs-integrated.json").is_file())
        self.assertFalse(list(self.ksun.rglob("*.rej")))

    def test_changed_reject_fingerprint_is_fatal(self) -> None:
        def fake_patch(tree: Path, patch_file: Path):
            self._make_rejects({"kernel/unexpected.c.rej"})
            return 1, "changed"

        with mock.patch.object(HELPER, "_gnu_patch_version", return_value="GNU patch 2.7.6"), mock.patch.object(
            HELPER, "_run_patch", side_effect=fake_patch
        ):
            with self.assertRaisesRegex(HELPER.IntegrationError, "fingerprint changed"):
                HELPER.integrate(self.ksun, self.susfs, self.wild)

    def test_preexisting_patch_residue_is_fatal(self) -> None:
        residue = self.ksun / "kernel" / "old.orig"
        residue.write_text("old\n", encoding="utf-8")
        with self.assertRaisesRegex(HELPER.IntegrationError, "not clean"):
            HELPER.integrate(self.ksun, self.susfs, self.wild)


if __name__ == "__main__":
    unittest.main()
