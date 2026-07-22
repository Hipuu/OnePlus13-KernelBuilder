from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "collect-patch-evidence.py"
SPEC = importlib.util.spec_from_file_location("collect_patch_evidence", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
HELPER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = HELPER
SPEC.loader.exec_module(HELPER)


class PatchFailureEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.debug = self.root / "debug"
        self.source.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_reject_and_backup_bytes_are_copied_with_digests(self) -> None:
        reject = self.source / "kernel_platform" / "common" / "driver.c.rej"
        backup = self.source / "kernel_platform" / "msm-kernel" / "driver.c.orig"
        reject.parent.mkdir(parents=True)
        backup.parent.mkdir(parents=True)
        reject_payload = b"@@ failed hunk @@\n"
        backup_payload = b"original driver bytes\n"
        reject.write_bytes(reject_payload)
        backup.write_bytes(backup_payload)
        (self.source / "ignored.rej.txt").write_bytes(b"ignored")

        document = HELPER.collect(self.source, self.debug)

        self.assertEqual(document["status"], "captured")
        self.assertEqual(document["file_count"], 2)
        records = {record["source_path"]: record for record in document["files"]}
        expected = {
            "kernel_platform/common/driver.c.rej": reject_payload,
            "kernel_platform/msm-kernel/driver.c.orig": backup_payload,
        }
        self.assertEqual(set(records), set(expected))
        for relative, payload in expected.items():
            with self.subTest(relative=relative):
                record = records[relative]
                self.assertEqual(record["size"], len(payload))
                self.assertEqual(record["sha256"], hashlib.sha256(payload).hexdigest())
                self.assertEqual(
                    (self.debug / "patch-residue" / record["evidence_path"]).read_bytes(),
                    payload,
                )
        manifest = json.loads(
            (
                self.debug
                / "patch-residue"
                / "PATCH-RESIDUE-MANIFEST.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(manifest, document)

    def test_empty_collection_is_explicit_and_stale_destination_is_rejected(self) -> None:
        document = HELPER.collect(self.source, self.debug)
        self.assertEqual(document["file_count"], 0)
        self.assertEqual(document["files"], [])
        with self.assertRaisesRegex(HELPER.CollectionError, "already exists"):
            HELPER.collect(self.source, self.debug)

    def test_partial_staging_is_removed_and_collection_can_be_retried(self) -> None:
        (self.source / "failure.rej").write_bytes(b"failed hunk")
        original_write = HELPER._write_exclusive

        def fail_after_write(path: Path, payload: bytes) -> None:
            original_write(path, payload)
            if path.suffix == ".rej":
                raise HELPER.CollectionError("simulated evidence write failure")

        with mock.patch.object(HELPER, "_write_exclusive", side_effect=fail_after_write):
            with self.assertRaisesRegex(
                HELPER.CollectionError,
                "simulated evidence write failure",
            ):
                HELPER.collect(self.source, self.debug)
        self.assertFalse((self.debug / "patch-residue").exists())
        self.assertEqual(list(self.debug.glob(".patch-residue.*")), [])

        document = HELPER.collect(self.source, self.debug)
        self.assertEqual(document["file_count"], 1)

    def test_cleanup_failure_is_not_allowed_to_mask_capture_failure(self) -> None:
        (self.source / "failure.rej").write_bytes(b"failed hunk")
        with mock.patch.object(
            HELPER,
            "_write_exclusive",
            side_effect=HELPER.CollectionError("primary capture failure"),
        ), mock.patch.object(
            HELPER,
            "_remove_staging_directory",
            side_effect=HELPER.CollectionError("secondary cleanup failure"),
        ):
            with self.assertRaisesRegex(
                HELPER.CollectionError,
                "primary capture failure",
            ) as raised:
                HELPER.collect(self.source, self.debug)
        self.assertTrue(
            any(
                "staging cleanup also failed: secondary cleanup failure" in note
                for note in getattr(raised.exception, "__notes__", [])
            )
        )

    def test_symlink_residue_and_size_overflow_fail_closed(self) -> None:
        outside = self.root / "outside"
        outside.write_bytes(b"outside")
        link = self.source / "leak.rej"
        try:
            link.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"symlink creation is unavailable: {exc}")
        with self.assertRaisesRegex(
            HELPER.CollectionError,
            "regular file|junction or reparse",
        ):
            HELPER.collect(self.source, self.root / "debug-symlink")

        link.unlink()
        original_limit = HELPER.MAX_MEMBER_BYTES
        try:
            HELPER.MAX_MEMBER_BYTES = 3
            (self.source / "large.rej").write_bytes(b"four")
            with self.assertRaisesRegex(HELPER.CollectionError, "exceeds 3 bytes"):
                HELPER.collect(self.source, self.root / "debug-size")
        finally:
            HELPER.MAX_MEMBER_BYTES = original_limit

    def test_hardlinked_residue_and_symlinked_output_fail_closed(self) -> None:
        original = self.source / "original"
        original.write_bytes(b"hard-linked bytes")
        linked = self.source / "hardlink.rej"
        try:
            linked.hardlink_to(original)
        except OSError as exc:
            self.skipTest(f"hard-link creation is unavailable: {exc}")
        with self.assertRaisesRegex(HELPER.CollectionError, "hard-linked"):
            HELPER.collect(self.source, self.root / "debug-hardlink")

        linked.unlink()
        outside = self.root / "outside-debug"
        outside.mkdir()
        linked_debug = self.root / "debug-link"
        try:
            linked_debug.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlink creation is unavailable: {exc}")
        with self.assertRaisesRegex(
            HELPER.CollectionError,
            "must not traverse a symlink|junction or reparse",
        ):
            HELPER.collect(self.source, linked_debug)

    def test_workflows_collect_after_attempt_even_when_patch_application_fails(self) -> None:
        build = (ROOT / ".github" / "workflows" / "build.yml").read_text(
            encoding="utf-8"
        )
        apply_start = build.index("      - name: Apply selected patch series\n")
        collect_start = build.index("      - name: Collect patch residue evidence\n")
        capture_start = build.index("      - name: Capture validated KMI patch evidence\n")
        self.assertLess(apply_start, collect_start)
        self.assertLess(collect_start, capture_start)
        build_step = build[collect_start:capture_start]
        self.assertIn(
            "if: always() && steps.apply_patches.outcome != 'skipped'", build_step
        )
        self.assertIn("python3 scripts/collect-patch-evidence.py", build_step)
        self.assertIn('--source-dir "$SOURCE_DIR"', build_step)
        self.assertIn('--output-dir "$DEBUG_DIR"', build_step)

        validate = (ROOT / ".github" / "workflows" / "validate.yml").read_text(
            encoding="utf-8"
        )
        apply_start = validate.index(
            "      - name: Apply full patch series on disposable checkout\n"
        )
        collect_start = validate.index(
            "      - name: Collect patch rehearsal residue evidence\n"
        )
        configure_start = validate.index(
            "      - name: Configure full mixed target on patched checkout\n"
        )
        self.assertLess(apply_start, collect_start)
        self.assertLess(collect_start, configure_start)
        validate_step = validate[collect_start:configure_start]
        self.assertIn(
            "if: always() && steps.apply_full_patches.outcome != 'skipped'",
            validate_step,
        )
        self.assertIn("python3 scripts/collect-patch-evidence.py", validate_step)
        upload_start = validate.index("      - name: Upload patch diagnostics\n")
        upload_step = validate[upload_start:]
        self.assertIn(
            "name: patch-rehearsal-${{ matrix.base }}-${{ matrix.root }}-"
            "${{ github.run_id }}-attempt-${{ github.run_attempt }}",
            upload_step,
        )
        self.assertIn("          include-hidden-files: true\n", upload_step)


if __name__ == "__main__":
    unittest.main()
