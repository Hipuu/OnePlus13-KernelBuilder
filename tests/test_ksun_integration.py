from __future__ import annotations

import importlib.util
import hashlib
import json
import subprocess
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
        (self.ksun / HELPER.STAGE_STAMP).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "variant": "kernelsu-next",
                    "source_commit": HELPER.EXPECTED_KSUN_COMMIT,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        hook_mode = self.ksun / HELPER.HOOK_MODE_PATH
        hook_mode.parent.mkdir(parents=True)
        hook_mode.write_text(HELPER.HOOK_MODE_STATEMENT + "\n", encoding="utf-8")
        susfs_patch = self.susfs / HELPER.SUSFS_PATCH
        susfs_patch.parent.mkdir(parents=True)
        susfs_patch.write_text("base\n", encoding="utf-8")
        fix_root = self.wild / HELPER.WILD_FIX_DIR
        fix_root.mkdir(parents=True)
        for name in HELPER.FIX_PATCHES:
            (fix_root / name).write_text(name + "\n", encoding="utf-8")
        for checkout in (self.susfs, self.wild):
            subprocess.run(["git", "init", "--quiet"], cwd=checkout, check=True)
            subprocess.run(["git", "config", "user.name", "Fixture"], cwd=checkout, check=True)
            subprocess.run(
                ["git", "config", "user.email", "fixture@example.invalid"], cwd=checkout, check=True
            )
            subprocess.run(["git", "add", "."], cwd=checkout, check=True)
            subprocess.run(["git", "commit", "--quiet", "-m", "fixture"], cwd=checkout, check=True)
        self.commit_patches = (
            mock.patch.object(HELPER, "EXPECTED_SUSFS_COMMIT", HELPER._git_head(self.susfs, "SUSFS")),
            mock.patch.object(HELPER, "EXPECTED_WILD_COMMIT", HELPER._git_head(self.wild, "Wild patch")),
        )
        susfs_payload = HELPER._git_blob_bytes(self.susfs, HELPER.SUSFS_PATCH, "SUSFS")
        self.susfs_hash_patch = mock.patch.object(
            HELPER, "EXPECTED_SUSFS_SHA256", hashlib.sha256(susfs_payload).hexdigest()
        )
        self.fix_hash_patch = mock.patch.dict(
            HELPER.EXPECTED_FIX_SHA256,
            {
                name: hashlib.sha256(
                    HELPER._git_blob_bytes(
                        self.wild, HELPER.WILD_FIX_DIR / name, f"Wild fix {name}"
                    )
                ).hexdigest()
                for name in HELPER.FIX_PATCHES
            },
        )
        self.susfs_hash_patch.start()
        self.fix_hash_patch.start()
        for patcher in self.commit_patches:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in reversed(self.commit_patches):
            patcher.stop()
        self.fix_hash_patch.stop()
        self.susfs_hash_patch.stop()
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
        self.assertEqual(
            document["reference"]["patch_order"],
            [HELPER.SUSFS_PATCH.name, *HELPER.FIX_PATCHES],
        )
        self.assertEqual(document["hook_mode"]["statement"], HELPER.HOOK_MODE_STATEMENT)
        self.assertEqual(document["inputs"]["kernelsu_next_commit"], HELPER.EXPECTED_KSUN_COMMIT)
        self.assertTrue((self.ksun / ".op13-susfs-integrated.json").is_file())
        self.assertFalse(list(self.ksun.rglob("*.rej")))

    def test_oneplus_reference_patch_sequence_is_locked(self) -> None:
        self.assertEqual(HELPER.REFERENCE_REPOSITORY, "Hipuu/OnePlus_KernelSU_SUSFS")
        self.assertEqual(
            HELPER.REFERENCE_COMMIT,
            "7ea1d5058255fba3cf8e836d0c6c27c9546b7f6c",
        )
        self.assertEqual(HELPER.EXPECTED_KSUN_COMMIT, "1a0ef4898568a013b51d74ceb5593b83725bfb78")
        self.assertEqual(
            HELPER.FIX_PATCHES,
            (
                "fix_Kbuild.patch",
                "fix_init.c.patch",
                "fix_kernel_umount.c.patch",
                "fix_sucompat.c.patch",
                "fix_setuid_hook.c.patch",
                "fix_supercall.c.patch",
                "overwrite_hook_mode.patch",
                "ksu_toolkit.patch",
            ),
        )
        self.assertEqual(
            HELPER.EXPECTED_OVERWRITE_HOOK_MODE_SHA256,
            "86c6bf22abd6a86577fa9d064a976f8eabd13cc649eaa4bf574a5f2b3f2ecde9",
        )

    def test_dependency_commit_drift_is_fatal(self) -> None:
        with mock.patch.object(HELPER, "EXPECTED_SUSFS_COMMIT", "0" * 40):
            with self.assertRaisesRegex(HELPER.IntegrationError, "SUSFS commit changed"):
                HELPER.integrate(self.ksun, self.susfs, self.wild)

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

    def test_patch_command_disables_mismatch_backups(self) -> None:
        completed = mock.Mock(returncode=1, stdout="expected partial application")
        with mock.patch.object(
            HELPER, "_patch_utility", return_value="patch"
        ), mock.patch.object(HELPER.subprocess, "run", return_value=completed) as run:
            return_code, output = HELPER._run_patch(self.ksun, self.susfs / HELPER.SUSFS_PATCH)
        command = run.call_args.args[0]
        self.assertIn("--no-backup-if-mismatch", command)
        self.assertEqual(return_code, 1)
        self.assertEqual(output, "expected partial application")

    def test_patch_utility_finds_git_for_windows_bundle(self) -> None:
        git = self.root / "Git" / "cmd" / "git.exe"
        patch = self.root / "Git" / "usr" / "bin" / "patch.exe"
        git.parent.mkdir(parents=True)
        patch.parent.mkdir(parents=True)
        git.write_bytes(b"git")
        patch.write_bytes(b"patch")

        def locate(name: str) -> str | None:
            return None if name == "patch" else str(git)

        with mock.patch.object(HELPER.shutil, "which", side_effect=locate):
            self.assertEqual(Path(HELPER._patch_utility()), patch.resolve())


if __name__ == "__main__":
    unittest.main()
