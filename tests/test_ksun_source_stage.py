from __future__ import annotations

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "stage_root_source", ROOT / "scripts" / "stage-root-source.py"
)
assert SPEC is not None and SPEC.loader is not None
HELPER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HELPER)


class KsunSourceStageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name) / "workspace"
        self.source = self.workspace / "dependency"
        self.source.mkdir(parents=True)
        self._git("init", "--quiet")
        self._git("config", "user.name", "Fixture")
        self._git("config", "user.email", "fixture@example.invalid")
        self._git("config", "core.autocrlf", "false")
        self._git("config", "core.symlinks", "false")
        (self.source / "kernel" / "include").mkdir(parents=True)
        (self.source / "kernel" / "Kconfig").write_text("config KSU\n", encoding="utf-8")
        (self.source / "kernel" / "Kbuild").write_text("obj-y += core.o\n", encoding="utf-8")
        (self.source / "kernel" / "core.c").write_text("int ksu_core;\n", encoding="utf-8")
        (self.source / "uapi").mkdir()
        for name in HELPER.EXPECTED_UAPI_FILES:
            (self.source / "uapi" / name).write_text(
                f"#define {name.replace('.', '_').upper()} 1\n", encoding="utf-8"
            )
        self.link_blob = self._index_symlink("kernel/include/uapi", "../../uapi")
        self._git("add", "kernel/Kconfig", "kernel/Kbuild", "kernel/core.c", "uapi")
        self._git("commit", "--quiet", "-m", "fixture")
        self.commit = self._git("rev-parse", "HEAD").stdout.strip()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _git(
        self,
        *arguments: str,
        input_bytes: bytes | None = None,
    ) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["git", "-C", str(self.source), *arguments],
            input=input_bytes,
            capture_output=True,
            text=input_bytes is None,
            check=True,
        )

    def _index_symlink(self, relative: str, target: str) -> str:
        path = self.source / Path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(target.encode("utf-8"))
        result = self._git("hash-object", "-w", "--stdin", input_bytes=target.encode("utf-8"))
        assert isinstance(result.stdout, bytes)
        blob = result.stdout.decode("ascii").strip()
        self._git("update-index", "--add", "--cacheinfo", f"120000,{blob},{relative}")
        return blob

    def _stage(self, destination: Path, variant: str = "kernelsu-next"):
        trees = {
            name: self._git("rev-parse", f"HEAD:{name}").stdout.strip()
            for name in ("kernel", "uapi")
        }
        with mock.patch.dict(HELPER.EXPECTED_COMMITS, {variant: self.commit}), mock.patch.dict(
            HELPER.EXPECTED_SOURCE_TREES, {variant: trees}
        ), mock.patch.object(HELPER, "EXPECTED_LINK_BLOB", self.link_blob):
            return HELPER.stage(self.workspace, self.source, destination, variant)

    def test_placeholder_link_is_materialized_with_deterministic_digest(self) -> None:
        first = self.workspace / "stage-one"
        second = self.workspace / "stage-two"
        first_document = self._stage(first)
        second_document = self._stage(second, "kernelsu")
        materialized = first / "kernel" / "include" / "uapi"
        self.assertTrue(materialized.is_dir())
        self.assertFalse(materialized.is_symlink())
        self.assertEqual((materialized / "ksu.h").read_text(encoding="utf-8"), "#define KSU_H 1\n")
        self.assertEqual(first_document["tree_sha256"], second_document["tree_sha256"])
        self.assertEqual(first_document["variant"], "kernelsu-next")
        self.assertEqual(second_document["variant"], "kernelsu")
        self.assertEqual(first_document["file_count"], 9)
        self.assertTrue((first / HELPER.STAMP_NAME).is_file())

    def test_changed_git_link_is_rejected(self) -> None:
        changed_blob = self._index_symlink("kernel/include/uapi", "../uapi")
        self._git("commit", "--quiet", "-m", "change target")
        self.commit = self._git("rev-parse", "HEAD").stdout.strip()
        with self.assertRaisesRegex(HELPER.StageError, "symlink contract changed"):
            self._stage(self.workspace / "changed")
        self.assertNotEqual(changed_blob, self.link_blob)

    def test_additional_git_link_is_rejected(self) -> None:
        self._index_symlink("kernel/unsafe", "../../outside")
        self._git("commit", "--quiet", "-m", "add unsafe link")
        self.commit = self._git("rev-parse", "HEAD").stdout.strip()
        with self.assertRaisesRegex(HELPER.StageError, "symlink contract changed"):
            self._stage(self.workspace / "additional")

    def test_dirty_checkout_and_destination_escape_are_rejected(self) -> None:
        (self.source / "kernel" / "core.c").write_text("int changed;\n", encoding="utf-8")
        with self.assertRaisesRegex(HELPER.StageError, "not byte-clean"):
            self._stage(self.workspace / "dirty")
        self._git("checkout", "--quiet", "--", "kernel/core.c")
        outside = self.workspace.parent / "outside"
        with self.assertRaisesRegex(HELPER.StageError, "escapes the workspace"):
            self._stage(outside)


if __name__ == "__main__":
    unittest.main()
