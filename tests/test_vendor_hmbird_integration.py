from __future__ import annotations

import contextlib
import dataclasses
import difflib
import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "integrate_vendor_hmbird",
    ROOT / "scripts" / "integrate-vendor-hmbird.py",
)
assert SPEC is not None and SPEC.loader is not None
HELPER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = HELPER
SPEC.loader.exec_module(HELPER)


class VendorHmbirdIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.builder = self.root / "builder"
        self.vendor = self.root / "msm-kernel"
        self.wild = self.root / "wild"
        self.compatibility_relative = Path("patches/hmbird/compat.patch")

        self.initial = {
            "arch/arm64/configs/defconfig": "CONFIG_SCHED_CLASS_EXT=y\n",
            "arch/arm64/configs/gki_defconfig": "CONFIG_SCHED_CLASS_EXT=y\n",
            "include/linux/sched.h": (
                "struct sched_ext_entity;\n"
                "#ifdef CONFIG_SLIM_SCHED\n"
                "struct task_struct { struct sched_ext_entity *scx; };\n"
                "#endif\n"
            ),
            "include/linux/sched/ext.h": "struct sched_ext_entity { int vendor; };\n",
            "init/init_task.c": "struct task init_task = { .scx = 0 };\n",
            "kernel/Kconfig.preempt": "config SCHED_CLASS_EXT\n\tbool \"ext\"\n",
            "kernel/sched/build_policy.c": '#include "ext.c"\n',
            "kernel/sched/core.c": (
                "void core(void)\n"
                "{\n"
                "\tSCHED_CHANGE_BLOCK();\n"
                "\t(void)&ext_sched_class;\n"
                "}\n"
            ),
            "kernel/sched/ext.c": "void ext_vendor(void) {}\n",
            "kernel/sched/ext.h": "void ext_vendor(void);\n",
        }
        self.compatible = {
            **self.initial,
            "include/linux/sched/ext.h": (
                "// compatibility comment\n"
                "struct sched_ext_entity { int vendor; };\n"
            ),
            "kernel/sched/ext.c": (
                "// compatibility comment\n"
                "void ext_vendor(void) {}\n"
            ),
            "kernel/sched/ext.h": (
                "// compatibility comment\n"
                "void ext_vendor(void);\n"
            ),
        }
        self.final = {
            **self.compatible,
            "arch/arm64/configs/defconfig": "CONFIG_HMBIRD_SCHED=y\n",
            "arch/arm64/configs/gki_defconfig": "CONFIG_HMBIRD_SCHED=y\n",
            "include/linux/sched.h": "struct task_struct { int hmbird; };\n",
            "init/init_task.c": "struct task init_task = { 0 };\n",
            "kernel/Kconfig.preempt": "config HMBIRD_SCHED\n\tbool \"hmbird\"\n",
            "kernel/sched/build_policy.c": '#include "hmbird/hmbird.c"\n',
            "kernel/sched/core.c": "void core(void) { hmbird_sched_class(); }\n",
            "kernel/sched/hmbird.h": "#define HMBIRD_HEADER 1\n",
            "kernel/sched/hmbird/hmbird.c": "void hmbird_sched_class(void) {}\n",
            "kernel/sched/hmbird/hmbird_sched.h": "#define HMBIRD_SCHED_HEADER 1\n",
        }
        for deleted in HELPER.SCX_WORKTREE_SHA256:
            self.final.pop(deleted)

        for relative, text in self.initial.items():
            self._write(self.vendor / relative, text)
        self._init_repository(self.vendor)
        self.vendor_commit = self._git(self.vendor, "rev-parse", "HEAD")

        compatibility_payload = self._make_patch(
            {path: self.initial[path] for path in HELPER.SCX_WORKTREE_SHA256},
            {path: self.compatible[path] for path in HELPER.SCX_WORKTREE_SHA256},
        )
        compatibility_path = self.builder / self.compatibility_relative
        compatibility_path.parent.mkdir(parents=True, exist_ok=True)
        compatibility_path.write_bytes(compatibility_payload)

        main_payload = self._make_patch(self.compatible, self.final)
        self._write_bytes(self.wild / "main.patch", main_payload)
        self._init_repository(self.wild)
        self.wild_commit = self._git(self.wild, "rev-parse", "HEAD")

        self.scx_sha256 = {
            relative: hashlib.sha256((self.vendor / relative).read_bytes()).hexdigest()
            for relative in HELPER.SCX_WORKTREE_SHA256
        }
        preimages = {
            relative: self._git(self.vendor, "rev-parse", f"HEAD:{relative}")
            for relative in HELPER.SCX_WORKTREE_SHA256
        }
        outputs = {
            relative: hashlib.sha256(text.encode("utf-8")).hexdigest()
            for relative, text in self.final.items()
            if relative.startswith("kernel/sched/hmbird")
        }
        self.spec = HELPER.BaseSpec(
            vendor_commit=self.vendor_commit,
            main_patch=Path("main.patch"),
            main_sha256=hashlib.sha256(main_payload).hexdigest(),
            compatibility_patch=self.compatibility_relative,
            compatibility_sha256=hashlib.sha256(compatibility_payload).hexdigest(),
            preimage_blobs=preimages,
            output_sha256=outputs,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")

    @staticmethod
    def _write_bytes(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

    def _git(self, repository: Path, *arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return result.stdout.strip()

    @staticmethod
    def _git_bytes(repository: Path, *arguments: str) -> bytes:
        result = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return result.stdout

    def _init_repository(self, repository: Path) -> None:
        self._git(repository, "init", "-q")
        self._git(repository, "config", "user.name", "Fixture")
        self._git(repository, "config", "user.email", "fixture@example.invalid")
        self._git(repository, "config", "core.autocrlf", "false")
        self._git(repository, "add", ".")
        self._git(repository, "commit", "-q", "-m", "fixture")

    @staticmethod
    def _make_patch(before: dict[str, str], after: dict[str, str]) -> bytes:
        output: list[str] = []
        for relative in sorted(set(before) | set(after)):
            old = before.get(relative)
            new = after.get(relative)
            if old == new:
                continue
            output.append(f"diff --git a/{relative} b/{relative}\n")
            if old is None:
                output.append("new file mode 100644\n")
            if new is None:
                output.append("deleted file mode 100644\n")
            output.extend(
                difflib.unified_diff(
                    [] if old is None else old.splitlines(keepends=True),
                    [] if new is None else new.splitlines(keepends=True),
                    fromfile="/dev/null" if old is None else f"a/{relative}",
                    tofile="/dev/null" if new is None else f"b/{relative}",
                    n=3,
                )
            )
        return "".join(output).encode("utf-8")

    @contextlib.contextmanager
    def _fixture_pins(self, spec: object | None = None):
        selected = self.spec if spec is None else spec
        with mock.patch.object(HELPER, "BASE_SPECS", {"fixture": selected}), mock.patch.object(
            HELPER, "EXPECTED_WILD_COMMIT", self.wild_commit
        ), mock.patch.object(HELPER, "SCX_WORKTREE_SHA256", self.scx_sha256):
            yield

    def test_applies_compatibility_then_unchanged_main_patch_with_zero_fuzz(self) -> None:
        pinned_main = self._git_bytes(self.wild, "show", "HEAD:main.patch")
        with self._fixture_pins():
            document = HELPER.integrate(
                self.vendor,
                self.wild,
                "fixture",
                repository_root=self.builder,
            )

        self.assertEqual(document["inputs"]["vendor_commit"], self.vendor_commit)
        self.assertEqual(document["inputs"]["wild_commit"], self.wild_commit)
        self.assertEqual(document["patch_tool"]["fuzz"], 0)
        self.assertTrue(document["patch_tool"]["forward_only"])
        self.assertEqual(
            document["inputs"]["main_patch_sha256"],
            hashlib.sha256(pinned_main).hexdigest(),
        )
        self.assertEqual(self._git_bytes(self.wild, "show", "HEAD:main.patch"), pinned_main)
        for relative in self.scx_sha256:
            self.assertFalse((self.vendor / relative).exists())
        for relative, expected in self.final.items():
            self.assertEqual((self.vendor / relative).read_text(encoding="utf-8"), expected)
        stamp = self.vendor / HELPER.STAMP_NAME
        self.assertEqual(json.loads(stamp.read_text(encoding="utf-8")), document)
        self.assertFalse(list(self.vendor.rglob("*.rej")))
        self.assertFalse(list(self.vendor.rglob("*.orig")))

    def test_wrong_vendor_commit_stops_before_mutation(self) -> None:
        self._write(self.vendor / "README", "new commit\n")
        self._git(self.vendor, "add", "README")
        self._git(self.vendor, "commit", "-q", "-m", "wrong commit")
        before = {
            relative: (self.vendor / relative).read_bytes()
            for relative in self.initial
        }
        with self._fixture_pins(), self.assertRaisesRegex(
            HELPER.IntegrationError, "vendor kernel commit changed"
        ):
            HELPER.integrate(
                self.vendor,
                self.wild,
                "fixture",
                repository_root=self.builder,
            )
        self.assertEqual(
            before,
            {relative: (self.vendor / relative).read_bytes() for relative in self.initial},
        )
        self.assertFalse((self.vendor / HELPER.STAMP_NAME).exists())

    def test_projects_a_pinned_common_header_into_the_vendor_tree(self) -> None:
        common = self.root / "common"
        relative = "include/linux/sched/hmbird.h"
        payload = b"#ifndef FIXTURE_HMBIRD_H\n#define FIXTURE_HMBIRD_H\n#endif\n"
        self._write_bytes(common / relative, payload)
        self._init_repository(common)
        common_commit = self._git(common, "rev-parse", "HEAD")
        common_blob = self._git(common, "rev-parse", f"HEAD:{relative}")
        common_sha256 = hashlib.sha256(payload).hexdigest()
        spec = dataclasses.replace(
            self.spec,
            output_sha256={**self.spec.output_sha256, relative: common_sha256},
            common_commit=common_commit,
            common_projection_blobs={relative: common_blob},
            common_projection_sha256={relative: common_sha256},
        )

        with self._fixture_pins(spec):
            document = HELPER.integrate(
                self.vendor,
                self.wild,
                "fixture",
                common_dir=common,
                repository_root=self.builder,
            )

        self.assertEqual((self.vendor / relative).read_bytes(), payload)
        self.assertEqual(document["inputs"]["common_commit"], common_commit)
        self.assertEqual(document["inputs"]["common_projection_blobs"], {relative: common_blob})
        self.assertEqual(
            document["inputs"]["common_projection_sha256"],
            {relative: common_sha256},
        )

    def test_required_common_projection_stops_before_vendor_mutation(self) -> None:
        relative = "include/linux/sched/hmbird.h"
        spec = dataclasses.replace(
            self.spec,
            common_commit="1" * 40,
            common_projection_blobs={relative: "2" * 40},
            common_projection_sha256={relative: "3" * 64},
        )
        before_status = self._git(self.vendor, "status", "--porcelain")
        with self._fixture_pins(spec), self.assertRaisesRegex(
            HELPER.IntegrationError, "requires the pinned common kernel checkout"
        ):
            HELPER.integrate(
                self.vendor,
                self.wild,
                "fixture",
                repository_root=self.builder,
            )
        self.assertEqual(self._git(self.vendor, "status", "--porcelain"), before_status)

    def test_main_patch_hash_and_scx_worktree_preimages_are_pinned(self) -> None:
        wrong = dataclasses.replace(self.spec, main_sha256="0" * 64)
        with self._fixture_pins(wrong), self.assertRaisesRegex(
            HELPER.IntegrationError, "Wild Fengchi patch changed"
        ):
            HELPER.integrate(
                self.vendor,
                self.wild,
                "fixture",
                repository_root=self.builder,
            )

        scx = self.vendor / "kernel/sched/ext.c"
        scx.write_text("changed locally\n", encoding="utf-8", newline="\n")
        with self._fixture_pins(), self.assertRaisesRegex(
            HELPER.IntegrationError, "vendor SCX preimage kernel/sched/ext.c changed"
        ):
            HELPER.integrate(
                self.vendor,
                self.wild,
                "fixture",
                repository_root=self.builder,
            )
        self.assertEqual(scx.read_text(encoding="utf-8"), "changed locally\n")

    def test_actual_phase_failure_rolls_back_all_patch_targets(self) -> None:
        before_status = self._git(self.vendor, "status", "--porcelain")
        original_assert = HELPER._assert_outputs
        calls = 0

        def fail_actual(tree: Path, spec: object, changes: object):
            nonlocal calls
            calls += 1
            result = original_assert(tree, spec, changes)
            if calls == 2:
                raise HELPER.IntegrationError("forced actual assertion failure")
            return result

        with self._fixture_pins(), mock.patch.object(
            HELPER, "_assert_outputs", side_effect=fail_actual
        ), self.assertRaisesRegex(HELPER.IntegrationError, "forced actual assertion failure"):
            HELPER.integrate(
                self.vendor,
                self.wild,
                "fixture",
                repository_root=self.builder,
            )

        self.assertEqual(self._git(self.vendor, "status", "--porcelain"), before_status)
        self.assertFalse((self.vendor / HELPER.STAMP_NAME).exists())
        self.assertFalse(list(self.vendor.rglob("*.rej")))
        self.assertFalse(list(self.vendor.rglob("*.orig")))

    def test_patch_runner_explicitly_disables_fuzz_without_a_shell(self) -> None:
        completed = subprocess.CompletedProcess(["patch"], 0, "patching file x\n", "")
        with mock.patch.object(HELPER.subprocess, "run", return_value=completed) as run:
            return_code, output = HELPER._run_patch_payload(
                self.vendor,
                b"diff --git a/x b/x\n",
                "fixture.patch",
                "patch",
            )
        self.assertEqual(return_code, 0)
        self.assertIn("patching file", output)
        command = run.call_args.args[0]
        self.assertIn("--fuzz=0", command)
        self.assertIn("--forward", command)
        self.assertIn("--reject-file=-", command)
        self.assertNotIn("--fuzz=1", command)
        self.assertEqual(run.call_args.kwargs["cwd"], self.vendor)
        self.assertFalse(run.call_args.kwargs["check"])

    def test_real_compatibility_patch_hashes_and_cli_document(self) -> None:
        for spec in HELPER.BASE_SPECS.values():
            payload = (ROOT / spec.compatibility_patch).read_bytes()
            self.assertEqual(hashlib.sha256(payload).hexdigest(), spec.compatibility_sha256)
            changes = HELPER._patch_changes(payload, "real compatibility")
            self.assertEqual(
                {path.as_posix() for path in changes.targets},
                set(spec.preimage_blobs),
            )

        expected = {"base": "oos16", "schema_version": 1}
        output = io.StringIO()
        with mock.patch.object(HELPER, "integrate", return_value=expected) as integrate, contextlib.redirect_stdout(
            output
        ):
            result = HELPER.main(
                [
                    "--source-dir",
                    str(self.vendor),
                    "--wild-dir",
                    str(self.wild),
                    "--base",
                    "oos16",
                ]
            )
        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue(), json.dumps(expected, indent=2, sort_keys=True) + "\n")
        integrate.assert_called_once_with(
            self.vendor,
            self.wild,
            "oos16",
            common_dir=None,
            repository_root=None,
            stamp=None,
        )


if __name__ == "__main__":
    unittest.main()
