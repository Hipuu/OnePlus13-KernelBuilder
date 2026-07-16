from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relative: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


STAGE = _load("stage_root_source_contract", "scripts/stage-root-source.py")
PIN = _load("pin_root_version_contract", "scripts/pin-root-version.py")
INTEGRATE = _load("integrate_ksun_susfs_contract", "scripts/integrate-ksun-susfs.py")
INSTALL = _load("install_root_driver_contract", "scripts/install-root-driver.py")


class RootCompatibilityContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.lock = json.loads((ROOT / "dependencies" / "lock.yml").read_text(encoding="utf-8"))
        cls.root_series = json.loads(
            (ROOT / "patches" / "series" / "root.yml").read_text(encoding="utf-8")
        )

    def test_locked_root_commits_match_every_exact_source_contract(self) -> None:
        dependencies = self.lock["dependencies"]
        expected = {
            "kernelsu": dependencies["kernelsu"]["commit"],
            "kernelsu-next": dependencies["kernelsu_next"]["commit"],
        }
        self.assertEqual(STAGE.EXPECTED_COMMITS, expected)
        self.assertEqual(PIN.PINS["kernelsu"]["commit"], expected["kernelsu"])
        self.assertEqual(PIN.PINS["kernelsu-next"]["commit"], expected["kernelsu-next"])
        self.assertEqual(INTEGRATE.EXPECTED_KSUN_COMMIT, expected["kernelsu-next"])
        self.assertEqual(INTEGRATE.EXPECTED_SUSFS_COMMIT, dependencies["susfs"]["commit"])
        self.assertEqual(
            INTEGRATE.EXPECTED_WILD_COMMIT,
            dependencies["wild_kernel_patches"]["commit"],
        )

    def test_oneplus_susfs_patch_order_and_blobs_are_cross_file_locked(self) -> None:
        operations = {operation["id"]: operation for operation in self.root_series["operations"]}
        for operation_id in ("stage-kernelsu", "stage-kernelsu-next"):
            argv = operations[operation_id]["argv"]
            source_root = argv.index("--source-root")
            self.assertEqual(argv[source_root + 1], "{cache_root}/git")
        classic = operations["patch-kernelsu-susfs"]
        self.assertEqual(classic["dependency"], "susfs")
        self.assertEqual(classic["sha256"], INTEGRATE.EXPECTED_SUSFS_SHA256)
        self.assertEqual(classic["cwd"], "kernel_platform/KernelSU")
        self.assertEqual(classic["path"], "kernel_patches/KernelSU/10_enable_susfs_for_ksu.patch")
        # The pinned upstream patch is CRLF text.  Route it through the same
        # GNU patch family used by the OnePlus reference so trailing CRs are
        # normalized and the untracked staged driver cannot be skipped by an
        # enclosing Git checkout.  The exact post-patch hashes remain pinned
        # by pin-root-version.py.
        self.assertEqual(classic["fuzz"], 1)
        self.assertEqual(
            INTEGRATE.FIX_PATCHES,
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
            INTEGRATE.EXPECTED_FIX_SHA256["overwrite_hook_mode.patch"],
            INTEGRATE.EXPECTED_OVERWRITE_HOOK_MODE_SHA256,
        )

    def test_final_driver_contracts_cover_both_selectable_variants(self) -> None:
        self.assertEqual(set(INSTALL.EXPECTED_TREES), set(STAGE.EXPECTED_COMMITS))
        self.assertEqual(
            INSTALL.EXPECTED_TREES["kernelsu"],
            {
                "file_count": 91,
                "tree_sha256": "c32526dbc9392b46ee6f516abfcde1adf72cfc87ea2f8c7f32b1be2fa87a5c69",
            },
        )
        self.assertEqual(
            INSTALL.EXPECTED_TREES["kernelsu-next"],
            {
                "file_count": 92,
                "tree_sha256": "7047e1e47aef84f8740f3aeef7908d2a251d61aab737dfcf699297b90cc98208",
            },
        )


if __name__ == "__main__":
    unittest.main()
