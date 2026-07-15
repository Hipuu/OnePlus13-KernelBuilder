from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "integrate_kernel_susfs", ROOT / "scripts" / "integrate-kernel-susfs.py"
)
assert SPEC is not None and SPEC.loader is not None
HELPER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HELPER)


class KernelSusfsIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.common = self.root / "common"
        self.susfs = self.root / "susfs"
        for directory in (
            self.common / "drivers/input",
            self.common / "fs/proc",
            self.common / "include/linux",
            self.common / "mm",
            self.susfs / "kernel_patches/fs",
            self.susfs / "kernel_patches/include/linux",
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self._write(
            self.common / "Makefile",
            "VERSION = 6\nPATCHLEVEL = 6\nSUBLEVEL = 99\n",
        )
        self._write(self.common / "drivers/input/input.c", "void input_handle_event(void) {}\n")
        self._write(self.common / "fs/Makefile", "obj-y := open.o\n")
        self._write(
            self.common / "fs/namespace.c",
            '#include <trace/hooks/blk.h>\n\nstatic int mnt_alloc_id(void) { return 0; }\n',
        )
        self._write(
            self.common / "fs/proc/base.c",
            '#include <linux/cpufreq_times.h>\n#include <trace/events/oom.h>\n',
        )
        self._write(
            self.common / "mm/memory.c",
            '#include <linux/sched/sysctl.h>\n#include <trace/events/kmem.h>\n',
        )
        self._write(
            self.common / "fs/proc/task_mmu.c",
            "static int show_smaps_rollup(void)\n"
            "{\n"
            f"{HELPER.LAST_VMA_COMPACT}\n"
            "\treturn 0;\n"
            "}\n\n"
            "static ssize_t pagemap_read(void)\n"
            "{\n"
            f"{HELPER.TASK_MARKER}\n"
            "\treturn ret + copied;\n"
            "}\n",
        )
        self._write(
            self.susfs / HELPER.SUSFS_C_RELATIVE,
            "#ifdef CONFIG_KSU_SUSFS_SUS_PATH\nvoid susfs_init(void) {}\n#endif\n",
        )
        self._write(
            self.susfs / "kernel_patches/include/linux/susfs.h",
            '#define SUSFS_VERSION "v2.2.0"\nvoid susfs_init(void);\n',
        )
        self._write(
            self.susfs / "kernel_patches/include/linux/susfs_def.h",
            "#define SUSFS_IS_INODE_SUS_MAP(inode) (false)\n",
        )
        patch_targets = (
            "drivers/input/input.c",
            "fs/Makefile",
            "fs/namespace.c",
            "fs/proc/base.c",
            "fs/proc/task_mmu.c",
            "mm/memory.c",
        )
        patch_text = "\n".join(
            f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}"
            for path in patch_targets
        )
        self._write(self.susfs / HELPER.PATCH_RELATIVE, patch_text + "\n")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _write(path: Path, value: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8", newline="\n")

    def _fake_success(self, tree: Path, patch_file: Path) -> tuple[int, str]:
        self.assertEqual(tree, self.common.resolve())
        self.assertEqual(patch_file, (self.susfs / HELPER.PATCH_RELATIVE).resolve())
        namespace = (tree / "fs/namespace.c").read_text(encoding="utf-8")
        proc_base = (tree / "fs/proc/base.c").read_text(encoding="utf-8")
        memory = (tree / "mm/memory.c").read_text(encoding="utf-8")
        task_mmu = (tree / "fs/proc/task_mmu.c").read_text(encoding="utf-8")
        self.assertIn(f"{HELPER.TRACE_BLK}\n{HELPER.TRACE_FS}\n", namespace)
        self.assertIn(f"{HELPER.CPUFREQ_TIMES}\n{HELPER.DMA_BUF}\n", proc_base)
        self.assertIn(f"{HELPER.SCHED_SYSCTL}\n{HELPER.ZSWAP}\n", memory)
        self.assertIn(HELPER.TASK_CONTEXT, task_mmu)
        self.assertIn(HELPER.LAST_VMA_EXPANDED, task_mmu)
        with (tree / "fs/Makefile").open("a", encoding="utf-8", newline="\n") as handle:
            handle.write("obj-$(CONFIG_KSU_SUSFS) += susfs.o\n")
        with (tree / "fs/namespace.c").open("a", encoding="utf-8", newline="\n") as handle:
            handle.write("#ifdef CONFIG_KSU_SUSFS_SUS_MOUNT\n#endif\n")
        with (tree / "fs/proc/task_mmu.c").open("a", encoding="utf-8", newline="\n") as handle:
            handle.write("#ifdef CONFIG_KSU_SUSFS_SUS_MAP\n#endif\n")
        return 0, "patching file fs/Makefile\n"

    def test_prepares_applies_restores_and_records_hashes(self) -> None:
        with mock.patch.object(HELPER, "_gnu_patch_version", return_value="GNU patch 2.7.6"), mock.patch.object(
            HELPER, "_run_patch", side_effect=self._fake_success
        ):
            document = HELPER.integrate(self.common, self.susfs, "oos16")

        task_mmu = (self.common / "fs/proc/task_mmu.c").read_text(encoding="utf-8")
        self.assertNotIn(HELPER.TASK_DECLARATIONS, task_mmu)
        self.assertIn(HELPER.LAST_VMA_COMPACT, task_mmu)
        self.assertNotIn(HELPER.LAST_VMA_EXPANDED, task_mmu)
        self.assertIn(HELPER.TRACE_FS, (self.common / "fs/namespace.c").read_text(encoding="utf-8"))
        self.assertIn(HELPER.DMA_BUF, (self.common / "fs/proc/base.c").read_text(encoding="utf-8"))
        self.assertIn(HELPER.ZSWAP, (self.common / "mm/memory.c").read_text(encoding="utf-8"))
        self.assertEqual(document["base"], "oos16")
        self.assertEqual(
            document["compatibility"],
            {
                "trace_hooks_fs_inserted": True,
                "dma_buf_inserted": True,
                "zswap_inserted": True,
                "task_mmu_declarations_temporary": True,
                "last_vma_end_expansion_temporary": True,
            },
        )
        copied_destinations = {record["destination"] for record in document["copied_files"]}
        self.assertEqual(
            copied_destinations,
            {"fs/susfs.c", "include/linux/susfs.h", "include/linux/susfs_def.h"},
        )
        for record in document["copied_files"]:
            destination = self.common / record["destination"]
            self.assertEqual(hashlib.sha256(destination.read_bytes()).hexdigest(), record["sha256"])
        stamp = self.common / HELPER.STAMP_NAME
        self.assertEqual(json.loads(stamp.read_text(encoding="utf-8")), document)
        self.assertFalse(list(self.common.rglob("*.rej")))
        self.assertFalse(list(self.common.rglob("*.orig")))

    def test_preexisting_context_is_not_removed(self) -> None:
        namespace = self.common / "fs/namespace.c"
        namespace.write_text(
            namespace.read_text(encoding="utf-8").replace(
                HELPER.TRACE_BLK, f"{HELPER.TRACE_BLK}\n{HELPER.TRACE_FS}"
            ),
            encoding="utf-8",
            newline="\n",
        )
        proc_base = self.common / "fs/proc/base.c"
        proc_base.write_text(
            proc_base.read_text(encoding="utf-8").replace(
                HELPER.CPUFREQ_TIMES, f"{HELPER.CPUFREQ_TIMES}\n{HELPER.DMA_BUF}"
            ),
            encoding="utf-8",
            newline="\n",
        )
        memory = self.common / "mm/memory.c"
        memory.write_text(
            memory.read_text(encoding="utf-8").replace(
                HELPER.SCHED_SYSCTL, f"{HELPER.SCHED_SYSCTL}\n{HELPER.ZSWAP}"
            ),
            encoding="utf-8",
            newline="\n",
        )
        task = self.common / "fs/proc/task_mmu.c"
        task.write_text(
            task.read_text(encoding="utf-8")
            .replace(HELPER.TASK_MARKER, HELPER.TASK_CONTEXT)
            .replace(HELPER.LAST_VMA_COMPACT, HELPER.LAST_VMA_EXPANDED),
            encoding="utf-8",
            newline="\n",
        )

        with mock.patch.object(HELPER, "_gnu_patch_version", return_value="GNU patch 2.7.6"), mock.patch.object(
            HELPER, "_run_patch", side_effect=self._fake_success
        ):
            document = HELPER.integrate(self.common, self.susfs, "oos15-global")

        final_task = task.read_text(encoding="utf-8")
        self.assertIn(HELPER.TASK_CONTEXT, final_task)
        self.assertIn(HELPER.LAST_VMA_EXPANDED, final_task)
        self.assertFalse(document["compatibility"]["task_mmu_declarations_temporary"])
        self.assertFalse(document["compatibility"]["last_vma_end_expansion_temporary"])

    def test_patch_failure_rolls_back_every_recorded_change(self) -> None:
        before = {
            path.relative_to(self.common): path.read_bytes()
            for path in self.common.rglob("*")
            if path.is_file()
        }

        def fail_patch(tree: Path, patch_file: Path) -> tuple[int, str]:
            (tree / "fs/Makefile").write_text("partially changed\n", encoding="utf-8", newline="\n")
            (tree / "fs/Makefile.rej").write_text("reject\n", encoding="utf-8", newline="\n")
            (tree / "fs/Makefile.orig").write_text("backup\n", encoding="utf-8", newline="\n")
            return 1, "Hunk #1 FAILED"

        with mock.patch.object(HELPER, "_gnu_patch_version", return_value="GNU patch 2.7.6"), mock.patch.object(
            HELPER, "_run_patch", side_effect=fail_patch
        ):
            with self.assertRaisesRegex(HELPER.IntegrationError, "failed with exit 1"):
                HELPER.integrate(self.common, self.susfs, "oos15-cn")

        after = {
            path.relative_to(self.common): path.read_bytes()
            for path in self.common.rglob("*")
            if path.is_file()
        }
        self.assertEqual(after, before)
        self.assertFalse((self.common / HELPER.STAMP_NAME).exists())

    def test_preexisting_residue_and_patch_path_escape_are_rejected(self) -> None:
        residue = self.common / "fs/old.orig"
        residue.write_text("old\n", encoding="utf-8", newline="\n")
        with self.assertRaisesRegex(HELPER.IntegrationError, "patch residue"):
            HELPER.integrate(self.common, self.susfs, "oos16")
        residue.unlink()
        self._write(
            self.susfs / HELPER.PATCH_RELATIVE,
            "diff --git a/../outside.c b/../outside.c\n--- a/../outside.c\n+++ b/../outside.c\n",
        )
        with self.assertRaisesRegex(HELPER.IntegrationError, "escapes its root"):
            HELPER.integrate(self.common, self.susfs, "oos16")

    def test_symlinked_dependency_input_is_rejected(self) -> None:
        implementation = self.susfs / HELPER.SUSFS_C_RELATIVE
        outside = self.root / "outside.c"
        outside.write_text("outside\n", encoding="utf-8", newline="\n")
        implementation.unlink()
        try:
            os.symlink(outside, implementation)
        except OSError as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")
        with self.assertRaisesRegex(HELPER.IntegrationError, "symlink"):
            HELPER.integrate(self.common, self.susfs, "oos16")

    def test_patch_runner_uses_gnu_patch_forward_without_a_shell(self) -> None:
        completed = subprocess.CompletedProcess(
            ["patch"], 0, "patching file fs/Makefile\n", ""
        )
        with mock.patch.object(HELPER.subprocess, "run", return_value=completed) as run:
            return_code, output = HELPER._run_patch(self.common, self.susfs / HELPER.PATCH_RELATIVE)
        self.assertEqual(return_code, 0)
        self.assertIn("patching file", output)
        run.assert_called_once_with(
            [
                "patch",
                "-p1",
                "--forward",
                "--batch",
                "--no-backup-if-mismatch",
                "--input",
                str(self.susfs / HELPER.PATCH_RELATIVE),
            ],
            cwd=self.common,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )

    def test_cli_prints_the_deterministic_document(self) -> None:
        expected = {"base": "oos16", "schema_version": 1}
        output = io.StringIO()
        with mock.patch.object(HELPER, "integrate", return_value=expected) as integrate, contextlib.redirect_stdout(output):
            result = HELPER.main(
                [
                    "--source-dir",
                    str(self.common),
                    "--susfs-dir",
                    str(self.susfs),
                    "--base",
                    "oos16",
                ]
            )
        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue(), json.dumps(expected, indent=2, sort_keys=True) + "\n")
        integrate.assert_called_once_with(self.common, self.susfs, "oos16")


if __name__ == "__main__":
    unittest.main()
