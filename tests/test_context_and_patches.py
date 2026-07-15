from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lib.config import discover_configs
from lib.context import load_context, new_context, validate_lineage, write_context
from lib.errors import BuildToolError
from lib.patches import apply_patch_series, validate_series_documents
from tests.support import make_repository


class ContextAndPatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = make_repository(Path(self.temporary.name))
        self.device, self.lock, self.profiles, self.features = discover_configs(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _context(self, profile_id: str = "oos16", *, smoke: bool = False) -> tuple[Path, Path]:
        source = self.root / "out" / "source"
        source.mkdir(parents=True, exist_ok=True)
        (source / "fixture.txt").write_text("before\n", encoding="utf-8", newline="\n")
        resolved = source / ".op13" / "resolved.xml"
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_bytes(self.profiles[profile_id].locked_manifest.read_bytes())
        path = source / ".op13" / "build-context.json"
        write_context(path, new_context(self.profiles[profile_id], self.lock, resolved, smoke=smoke))
        return source, path

    def test_context_digest_detects_tampering(self) -> None:
        _, path = self._context()
        data = json.loads(path.read_text(encoding="utf-8"))
        data["profile"] = "oos15-cn"
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(BuildToolError, "digest mismatch"):
            load_context(path)

    def test_cross_profile_lineage_is_rejected(self) -> None:
        _, path = self._context()
        context = load_context(path)
        with self.assertRaisesRegex(BuildToolError, "cross-profile mixing"):
            validate_lineage(context, self.profiles["oos15-cn"], self.lock)

    def test_ordered_replace_and_append_advance_context(self) -> None:
        source, path = self._context()
        records = apply_patch_series(
            root=self.root,
            source_dir=source,
            cache_root=self.root / ".cache" / "op13",
            context_path=path,
            profile=self.profiles["oos16"],
            feature=self.features["test"],
            lock=self.lock,
            root_variant="none",
            check_only=False,
            smoke=False,
            log_dir=self.root / "out" / "debug",
        )
        self.assertEqual([record["id"] for record in records], ["test:replace-token", "test:append-token"])
        self.assertEqual((source / "fixture.txt").read_text(encoding="utf-8"), "after\ntail\n")
        self.assertEqual(load_context(path)["stage"], "patches-applied")

    def test_replace_exact_count_prevents_fuzzy_edit(self) -> None:
        source, path = self._context()
        (source / "fixture.txt").write_text("before before\n", encoding="utf-8")
        with self.assertRaisesRegex(BuildToolError, "expected 1 occurrences"):
            apply_patch_series(
                root=self.root,
                source_dir=source,
                cache_root=self.root / ".cache" / "op13",
                context_path=path,
                profile=self.profiles["oos16"],
                feature=self.features["test"],
                lock=self.lock,
                root_variant="none",
                check_only=False,
                smoke=False,
                log_dir=self.root / "out" / "debug",
            )

    def test_every_series_dependency_must_be_locked(self) -> None:
        series = self.root / "patches" / "series" / "test.yml"
        data = json.loads(series.read_text(encoding="utf-8"))
        data["operations"] = [
            {
                "id": "bad",
                "type": "apply",
                "dependency": "unlocked",
                "path": "x.patch",
                "cwd": ".",
                "strip": 1,
            }
        ]
        series.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(BuildToolError, "unlocked dependency"):
            validate_series_documents(self.root, self.profiles, self.features, self.lock)

    def test_pinned_patch_digest_is_checked_before_application(self) -> None:
        source, context_path = self._context()
        patch = self.root / "patches" / "common" / "pinned.patch"
        patch.write_text(
            "diff --git a/fixture.txt b/fixture.txt\n"
            "--- a/fixture.txt\n"
            "+++ b/fixture.txt\n"
            "@@ -1 +1 @@\n"
            "-before\n"
            "+after\n",
            encoding="utf-8",
            newline="\n",
        )
        series = self.root / "patches" / "series" / "test.yml"
        data = json.loads(series.read_text(encoding="utf-8"))
        data["operations"] = [
            {
                "id": "pinned",
                "type": "apply",
                "path": "patches/common/pinned.patch",
                "sha256": hashlib.sha256(b"different bytes").hexdigest(),
                "cwd": ".",
                "strip": 1,
            }
        ]
        series.write_text(json.dumps(data), encoding="utf-8")

        with self.assertRaisesRegex(BuildToolError, "patch digest mismatch"):
            apply_patch_series(
                root=self.root,
                source_dir=source,
                cache_root=self.root / ".cache" / "op13",
                context_path=context_path,
                profile=self.profiles["oos16"],
                feature=self.features["test"],
                lock=self.lock,
                root_variant="none",
                check_only=False,
                smoke=False,
                log_dir=self.root / "out" / "debug",
            )
        self.assertEqual((source / "fixture.txt").read_text(encoding="utf-8"), "before\n")

    def test_explicit_fuzz_uses_audited_patch_path_and_records_output(self) -> None:
        source, context_path = self._context()
        (source / "fuzzy.txt").write_text(
            "actual-top\nkeep-a\nold\nkeep-b\nactual-bottom\n",
            encoding="utf-8",
            newline="\n",
        )
        patch = self.root / "patches" / "common" / "fuzzy.patch"
        patch.write_text(
            "diff --git a/fuzzy.txt b/fuzzy.txt\n"
            "--- a/fuzzy.txt\n"
            "+++ b/fuzzy.txt\n"
            "@@ -1,5 +1,5 @@\n"
            " expected-top\n"
            " keep-a\n"
            "-old\n"
            "+new\n"
            " keep-b\n"
            " expected-bottom\n",
            encoding="utf-8",
            newline="\n",
        )
        series = self.root / "patches" / "series" / "test.yml"
        data = json.loads(series.read_text(encoding="utf-8"))
        data["operations"] = [
            {
                "id": "fuzzy",
                "type": "apply",
                "path": "patches/common/fuzzy.patch",
                "cwd": ".",
                "strip": 1,
                "fuzz": 1,
            }
        ]
        series.write_text(json.dumps(data), encoding="utf-8")
        records = apply_patch_series(
            root=self.root,
            source_dir=source,
            cache_root=self.root / ".cache" / "op13",
            context_path=context_path,
            profile=self.profiles["oos16"],
            feature=self.features["test"],
            lock=self.lock,
            root_variant="none",
            check_only=False,
            smoke=False,
            log_dir=self.root / "out" / "debug",
        )
        self.assertEqual(
            (source / "fuzzy.txt").read_text(encoding="utf-8"),
            "actual-top\nkeep-a\nnew\nkeep-b\nactual-bottom\n",
        )
        self.assertEqual(records[0]["fuzz"], 1)
        self.assertIn("fuzz 1", records[0]["patch_output"])
        self.assertFalse(list(source.rglob("*.rej")))
        self.assertFalse(list(source.rglob("*.orig")))

    def test_fuzz_preflight_replays_sequential_new_file_diffs_without_mutation(self) -> None:
        source, context_path = self._context()
        patch = self.root / "patches" / "common" / "sequential.patch"
        patch.write_text(
            "diff --git a/generated.txt b/generated.txt\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/generated.txt\n"
            "@@ -0,0 +1 @@\n"
            "+alpha\n"
            "diff --git a/generated.txt b/generated.txt\n"
            "--- a/generated.txt\n"
            "+++ b/generated.txt\n"
            "@@ -1 +1 @@\n"
            "-alpha\n"
            "+beta\n",
            encoding="utf-8",
            newline="\n",
        )
        series = self.root / "patches" / "series" / "test.yml"
        data = json.loads(series.read_text(encoding="utf-8"))
        data["operations"] = [
            {
                "id": "sequential",
                "type": "apply",
                "path": "patches/common/sequential.patch",
                "cwd": ".",
                "strip": 1,
                "fuzz": 1,
            }
        ]
        series.write_text(json.dumps(data), encoding="utf-8")

        checked = apply_patch_series(
            root=self.root,
            source_dir=source,
            cache_root=self.root / ".cache" / "op13",
            context_path=context_path,
            profile=self.profiles["oos16"],
            feature=self.features["test"],
            lock=self.lock,
            root_variant="none",
            check_only=True,
            smoke=False,
            log_dir=self.root / "out" / "debug",
        )
        self.assertFalse((source / "generated.txt").exists())
        self.assertIn("preflight replay", checked[0]["patch_output"])

        apply_patch_series(
            root=self.root,
            source_dir=source,
            cache_root=self.root / ".cache" / "op13",
            context_path=context_path,
            profile=self.profiles["oos16"],
            feature=self.features["test"],
            lock=self.lock,
            root_variant="none",
            check_only=False,
            smoke=False,
            log_dir=self.root / "out" / "debug",
        )
        self.assertEqual((source / "generated.txt").read_text(encoding="utf-8"), "beta\n")
        self.assertFalse(list(source.rglob("*.rej")))
        self.assertFalse(list(source.rglob("*.orig")))


if __name__ == "__main__":
    unittest.main()
