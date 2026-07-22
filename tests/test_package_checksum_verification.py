from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify-package-checksums.py"
SPEC = importlib.util.spec_from_file_location("verify_package_checksums", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
HELPER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = HELPER
SPEC.loader.exec_module(HELPER)


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class PackageChecksumVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.dist = root / "dist"
        self.dist.mkdir()
        self.context = root / "build-context.json"
        self.files = {
            "BUILD-MANIFEST.json": b"{}\n",
            "kernel.zip": b"zip-bytes",
        }
        for name, payload in self.files.items():
            (self.dist / name).write_bytes(payload)
        checksum_payload = "".join(
            f"{digest(payload)}  {name}\n" for name, payload in sorted(self.files.items())
        ).encode("ascii")
        self.files[HELPER.CHECKSUM_NAME] = checksum_payload
        (self.dist / HELPER.CHECKSUM_NAME).write_bytes(checksum_payload)
        self.write_context()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_context(self) -> None:
        records = []
        for name, payload in self.files.items():
            records.append(
                {
                    "path": str((self.dist / name).resolve()),
                    "role": "checksums" if name == HELPER.CHECKSUM_NAME else "artifact",
                    "size": len(payload),
                    "sha256": digest(payload),
                }
            )
        self.context.write_text(
            json.dumps({"stage": "packaged", "packages": records}) + "\n",
            encoding="utf-8",
        )

    def test_exact_checksum_and_context_coverage_passes(self) -> None:
        result = HELPER.verify(self.dist, self.context)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["file_count"], 3)
        self.assertEqual(result["checksummed_file_count"], 2)

    def test_rewritten_checksum_is_rejected_by_sealed_context(self) -> None:
        path = self.dist / HELPER.CHECKSUM_NAME
        path.write_text(path.read_text(encoding="ascii").replace("  ", "  ./", 1), encoding="ascii")
        with self.assertRaises(HELPER.VerificationError):
            HELPER.verify(self.dist, self.context)

    def test_missing_extra_duplicate_unsorted_and_tampered_entries_fail(self) -> None:
        checksum = self.dist / HELPER.CHECKSUM_NAME
        original = checksum.read_bytes()
        cases = {
            "missing": original.splitlines(keepends=True)[0],
            "extra": original + f"{digest(b'x')}  extra.bin\n".encode("ascii"),
            "duplicate": original + original.splitlines(keepends=True)[0],
            "unsorted": b"".join(reversed(original.splitlines(keepends=True))),
            "tampered": (b"0" * 64) + original[64:],
        }
        for label, payload in cases.items():
            with self.subTest(label=label):
                checksum.write_bytes(payload)
                with self.assertRaises(HELPER.VerificationError):
                    HELPER.verify(self.dist, self.context)
                checksum.write_bytes(original)

    def test_context_coverage_digest_stage_and_checksum_role_fail_closed(self) -> None:
        original = json.loads(self.context.read_text(encoding="utf-8"))
        mutations = []
        missing = json.loads(json.dumps(original))
        missing["packages"].pop()
        mutations.append(missing)
        stale = json.loads(json.dumps(original))
        stale["packages"][0]["sha256"] = "0" * 64
        mutations.append(stale)
        wrong_stage = json.loads(json.dumps(original))
        wrong_stage["stage"] = "verified"
        mutations.append(wrong_stage)
        wrong_role = json.loads(json.dumps(original))
        for record in wrong_role["packages"]:
            if Path(record["path"]).name == HELPER.CHECKSUM_NAME:
                record["role"] = "artifact"
        mutations.append(wrong_role)
        for document in mutations:
            with self.subTest(document=document):
                self.context.write_text(json.dumps(document) + "\n", encoding="utf-8")
                with self.assertRaises(HELPER.VerificationError):
                    HELPER.verify(self.dist, self.context)

    def test_symlink_or_directory_package_members_are_rejected(self) -> None:
        directory = self.dist / "nested"
        directory.mkdir()
        with self.assertRaisesRegex(HELPER.VerificationError, "non-regular"):
            HELPER.verify(self.dist, self.context)

    def test_workflow_verifies_without_rewriting_checksum_file(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "build.yml").read_text(
            encoding="utf-8"
        )
        start = workflow.index("      - name: Verify packaged checksums and context\n")
        end = workflow.index("\n      - name: Collect diagnostics\n", start)
        step = workflow[start:end]
        self.assertIn("python3 scripts/verify-package-checksums.py", step)
        for mutator in ("sha256sum ", "mv ", "checksum_temporary", ": >"):
            with self.subTest(mutator=mutator):
                self.assertNotIn(mutator, step)


if __name__ == "__main__":
    unittest.main()
