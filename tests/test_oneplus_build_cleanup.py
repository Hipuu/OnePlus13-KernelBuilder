from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "integrate_oneplus_build_cleanup",
    ROOT / "scripts" / "integrate-oneplus-build-cleanup.py",
)
assert SPEC is not None and SPEC.loader is not None
HELPER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HELPER)


def _wrapper_source(*, guarded_remove: bool) -> str:
    remove = (
        "                        if os.path.exists(msm_f):\n"
        "                            os.remove(msm_f)"
        if guarded_remove
        else "                        os.remove(msm_f)"
    )
    return (
        r'''import logging
import os
import subprocess


class Builder:
    def __init__(self, workspace, during_build):
        self.workspace = str(workspace)
        self.gki_headers = True
        self.dry_run = False
        self.user_opts = []
        self.during_build = during_build

    def clean_legacy_generated_files(self):
        pass

    def build_targets(self, targets):
        self.during_build()

    def run_targets(self, targets):
        pass

    def build(self):
        targets_to_build = []

        if self.dry_run:
            self.user_opts.append("--nobuild")

        try:
            if self.gki_headers:
                gki_files_path = os.path.join(self.workspace, 'msm-kernel/files_gki_aarch64.txt')
                gki_f = open(gki_files_path, 'r')
                gki_files = gki_f.readlines()
                common_d = os.path.join(self.workspace, "common")
                msm_d = os.path.join(self.workspace, "msm-kernel")
                for f in gki_files:
                    if ".h" in f:
                        logging.info('GKI header file...%s', f)
                        f=f.strip()
                        common_f = os.path.join(common_d, f)
                        msm_f = os.path.join(msm_d, f)
__REMOVE_BLOCK__
                        os.symlink(common_f, msm_f)
                gki_f.close()

            logging.debug(
                "Building the following targets:\n%s",
                "\n".join([t.bazel_label for t in targets_to_build])
            )

            self.clean_legacy_generated_files()

            logging.info("Building targets...")
            self.build_targets(targets_to_build)

            if not self.dry_run:
                self.run_targets(targets_to_build)
        finally:
            if self.gki_headers:
                status = subprocess.Popen(["git", "checkout",
                                     "--pathspec-from-file=files_gki_aarch64.txt"],
                                    cwd=os.path.join(self.workspace, "msm-kernel"))
                status.wait()
                if status.returncode != 0:
                    logging.error("Failed to restore headers from symlinks")
                    logging.error("You might want to check your msm-kernel tree")
'''
        .replace("__REMOVE_BLOCK__", remove)
    )


def _expected_postimage(value: str) -> str:
    value = value.replace(
        '        try:\n'
        '            if self.gki_headers:\n',
        '        gki_headers_to_restore = []\n'
        '        try:\n'
        '            if self.gki_headers:\n',
        1,
    )
    value = value.replace(
        '                        f=f.strip()\n'
        '                        common_f = os.path.join(common_d, f)\n',
        '                        f=f.strip()\n'
        '                        gki_headers_to_restore.append(f)\n'
        '                        common_f = os.path.join(common_d, f)\n',
        1,
    )
    value = value.replace(
        '                status = subprocess.Popen(["git", "checkout",\n'
        '                                     "--pathspec-from-file=files_gki_aarch64.txt"],\n'
        '                                    cwd=os.path.join(self.workspace, "msm-kernel"))\n'
        '                status.wait()\n',
        '                status = subprocess.run(\n'
        '                    ["git", "checkout", "--pathspec-from-file=-"],\n'
        '                    cwd=os.path.join(self.workspace, "msm-kernel"),\n'
        '                    input="".join(\n'
        '                        f"{path}\\n" for path in gki_headers_to_restore\n'
        '                    ),\n'
        '                    text=True,\n'
        '                    check=False,\n'
        '                )\n',
        1,
    )
    return value


def _digest(value: str | bytes) -> str:
    payload = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(payload).hexdigest()


def _synthetic_contract(preimage: str, postimage: str) -> dict[str, dict[str, str]]:
    return {
        "oos16": {
            "commit": "7" * 40,
            "pre_sha256": _digest(preimage),
            "post_sha256": _digest(postimage),
        }
    }


class OnePlusBuildCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _write(path: Path, value: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8", newline="\n")

    def _integrate_synthetic(
        self,
        source: Path,
        *,
        guarded_remove: bool,
    ) -> tuple[str, str, dict[str, object]]:
        preimage = _wrapper_source(guarded_remove=guarded_remove)
        postimage = _expected_postimage(preimage)
        self._write(source / HELPER.TARGET_RELATIVE, preimage)
        with mock.patch.object(
            HELPER,
            "PROFILE_CONTRACTS",
            _synthetic_contract(preimage, postimage),
        ), mock.patch.object(HELPER, "_git_version", return_value="git version test"):
            document = HELPER.integrate(source, "oos16")
        return preimage, postimage, document

    def test_locked_preimages_postimages_and_patch_digest(self) -> None:
        expected = {
            "oos15-cn": (
                "d09a875fd283664a4ad3a8722fb608356985dab1",
                "492b95292dd8c0d3b8561eb97deb31b612ae4ba4c5a60f0da754ebd385621b59",
                "2e0611c2a56da02112c74f1f9fa9ee0d44baa35f0a6858035fa97cd5d9ee1568",
            ),
            "oos15-global": (
                "59336d4db04efdc70e1c63d6a92f7e4d14efafa8",
                "492b95292dd8c0d3b8561eb97deb31b612ae4ba4c5a60f0da754ebd385621b59",
                "2e0611c2a56da02112c74f1f9fa9ee0d44baa35f0a6858035fa97cd5d9ee1568",
            ),
            "oos16": (
                "73ecb0dc41fb28ce5727465bd19d7469b4a6db73",
                "8642456bbd6ea5bdf678bb80d8076df30738c7e10811e8a6b240a4017aba3676",
                "3fe60bcde6de22f72cde4b8d73dd3cae343d274b92572982f5f8b51393972327",
            ),
        }
        observed = {
            profile: (
                contract["commit"],
                contract["pre_sha256"],
                contract["post_sha256"],
            )
            for profile, contract in HELPER.PROFILE_CONTRACTS.items()
        }
        self.assertEqual(observed, expected)
        patch = ROOT / HELPER.PATCH_RELATIVE
        self.assertEqual(_digest(patch.read_bytes()), HELPER.PATCH_SHA256)

    def test_one_patch_accepts_oos15_and_oos16_cleanup_shapes(self) -> None:
        for guarded_remove in (False, True):
            with self.subTest(guarded_remove=guarded_remove):
                source = self.root / f"msm-{guarded_remove}"
                source.mkdir()
                preimage, postimage, document = self._integrate_synthetic(
                    source,
                    guarded_remove=guarded_remove,
                )
                self.assertEqual(
                    (source / HELPER.TARGET_RELATIVE).read_text(encoding="utf-8"),
                    postimage,
                )
                self.assertEqual(document["target"]["pre_sha256"], _digest(preimage))
                self.assertEqual(document["target"]["post_sha256"], _digest(postimage))
                self.assertTrue(document["cleanup"]["preserves_listed_non_headers"])
                stamp = source / HELPER.STAMP_NAME
                self.assertEqual(json.loads(stamp.read_text(encoding="utf-8")), document)

    def test_unknown_and_already_modified_inputs_fail_closed(self) -> None:
        preimage = _wrapper_source(guarded_remove=True)
        postimage = _expected_postimage(preimage)
        contract = _synthetic_contract(preimage, postimage)

        unknown = self.root / "unknown"
        unknown.mkdir()
        drifted = preimage + "# drift\n"
        self._write(unknown / HELPER.TARGET_RELATIVE, drifted)
        with mock.patch.object(HELPER, "PROFILE_CONTRACTS", contract):
            with self.assertRaisesRegex(HELPER.IntegrationError, "unrecognized or already-modified"):
                HELPER.integrate(unknown, "oos16")
        self.assertEqual(
            (unknown / HELPER.TARGET_RELATIVE).read_text(encoding="utf-8"),
            drifted,
        )
        self.assertFalse((unknown / HELPER.STAMP_NAME).exists())

        already = self.root / "already"
        already.mkdir()
        self._write(already / HELPER.TARGET_RELATIVE, postimage)
        with mock.patch.object(HELPER, "PROFILE_CONTRACTS", contract):
            with self.assertRaisesRegex(HELPER.IntegrationError, "already integrated"):
                HELPER.integrate(already, "oos16")
        self.assertFalse((already / HELPER.STAMP_NAME).exists())

    def test_failed_application_restores_exact_preimage(self) -> None:
        source = self.root / "rollback"
        source.mkdir()
        preimage = _wrapper_source(guarded_remove=True)
        postimage = _expected_postimage(preimage)
        target = source / HELPER.TARGET_RELATIVE
        self._write(target, preimage)

        def fail_apply(source_dir: Path, patch: Path, *, check_only: bool) -> str:
            self.assertTrue(os.path.samefile(source_dir, source))
            self.assertTrue(patch.is_file())
            if not check_only:
                target.write_text("partial\n", encoding="utf-8", newline="\n")
                raise HELPER.IntegrationError("synthetic apply failure")
            return ""

        with mock.patch.object(
            HELPER,
            "PROFILE_CONTRACTS",
            _synthetic_contract(preimage, postimage),
        ), mock.patch.object(HELPER, "_git_version", return_value="git version test"), mock.patch.object(
            HELPER, "_run_git_apply", side_effect=fail_apply
        ):
            with self.assertRaisesRegex(HELPER.IntegrationError, "synthetic apply failure"):
                HELPER.integrate(source, "oos16")

        self.assertEqual(target.read_text(encoding="utf-8"), preimage)
        self.assertFalse((source / HELPER.STAMP_NAME).exists())

    def test_cleanup_restores_only_symlinked_headers_and_preserves_modified_c(self) -> None:
        workspace = self.root / "kernel_platform"
        source = workspace / "msm-kernel"
        common = workspace / "common"
        source.mkdir(parents=True)
        common.mkdir()
        _, postimage, _ = self._integrate_synthetic(source, guarded_remove=True)

        common_header = common / "include/example.h"
        vendor_header = source / "include/example.h"
        vendor_c = source / "kernel/example.c"
        file_list = source / "files_gki_aarch64.txt"
        self._write(common_header, "common header\n")
        self._write(vendor_header, "vendor header\n")
        self._write(vendor_c, "vendor baseline\n")
        self._write(file_list, "include/example.h\nkernel/example.c\n")

        probe = self.root / "symlink-probe"
        try:
            os.symlink(common_header, probe)
        except OSError as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")
        else:
            probe.unlink()

        subprocess.run(["git", "init", "-q"], cwd=source, check=True)
        subprocess.run(
            ["git", "config", "core.autocrlf", "false"],
            cwd=source,
            check=True,
        )
        subprocess.run(
            ["git", "add", "include/example.h", "kernel/example.c"],
            cwd=source,
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=OnePlus cleanup test",
                "-c",
                "user.email=cleanup@example.invalid",
                "commit",
                "-q",
                "-m",
                "fixture",
            ],
            cwd=source,
            check=True,
        )
        self._write(vendor_c, "vendor SUSFS hook\n")

        module_path = source / HELPER.TARGET_RELATIVE
        self.assertEqual(module_path.read_text(encoding="utf-8"), postimage)
        module_spec = importlib.util.spec_from_file_location(
            "patched_oneplus_build_wrapper",
            module_path,
        )
        assert module_spec is not None and module_spec.loader is not None
        wrapper = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(wrapper)

        observed: list[str] = []

        def during_build() -> None:
            self.assertTrue(vendor_header.is_symlink())
            self.assertEqual(vendor_header.read_text(encoding="utf-8"), "common header\n")
            self.assertEqual(vendor_c.read_text(encoding="utf-8"), "vendor SUSFS hook\n")
            observed.append("built")

        wrapper.Builder(workspace, during_build).build()

        self.assertEqual(observed, ["built"])
        self.assertFalse(vendor_header.is_symlink())
        self.assertEqual(vendor_header.read_text(encoding="utf-8"), "vendor header\n")
        self.assertEqual(vendor_c.read_text(encoding="utf-8"), "vendor SUSFS hook\n")


if __name__ == "__main__":
    unittest.main()
