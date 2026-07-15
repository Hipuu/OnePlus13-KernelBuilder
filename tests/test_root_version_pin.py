from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("pin_root_version", ROOT / "scripts" / "pin-root-version.py")
assert SPEC is not None and SPEC.loader is not None
HELPER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HELPER)


class RootVersionPinTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name).resolve()
        self.root_dir = self.workspace / "kernel_platform" / "KernelSU"
        (self.root_dir / "kernel").mkdir(parents=True)
        self.commit = "a" * 40
        self.anchor = "ANCHOR\n"
        self.old_block = b"VERSION_DISCOVERY\nold fallback\n\n"
        self.original = b"head\n" + self.old_block + self.anchor.encode("utf-8") + b"tail\n"
        self.replacement = "PINNED_VERSION := 30001\n\n"
        self.updated = b"head\n" + self.replacement.encode("utf-8") + self.anchor.encode("utf-8") + b"tail\n"
        (self.root_dir / "kernel" / "Kbuild").write_bytes(self.original)
        (self.root_dir / HELPER.STAGE_STAMP).write_text(
            json.dumps({"variant": "kernelsu", "source_commit": self.commit}), encoding="utf-8"
        )
        self.pin_spec = {
            "commit": self.commit,
            "version": 30001,
            "tag": "vfixture",
            "history_count": 1,
            "pre_sha256": hashlib.sha256(self.original).hexdigest(),
            "block_start": "VERSION_DISCOVERY\n",
            "block_sha256": hashlib.sha256(self.old_block).hexdigest(),
            "replacement": self.replacement,
            "post_sha256": hashlib.sha256(self.updated).hexdigest(),
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _pin(self, variant: str = "kernelsu"):
        with mock.patch.dict(HELPER.PINS, {variant: self.pin_spec}), mock.patch.object(
            HELPER, "ANCHOR", self.anchor
        ):
            return HELPER.pin(self.workspace, self.root_dir, variant)

    def test_exact_version_block_is_pinned_and_stamped(self) -> None:
        document = self._pin()
        self.assertEqual((self.root_dir / "kernel" / "Kbuild").read_bytes(), self.updated)
        self.assertEqual(document["version"], 30001)
        self.assertEqual(document["kbuild_post_sha256"], hashlib.sha256(self.updated).hexdigest())
        stamped = json.loads((self.root_dir / HELPER.VERSION_STAMP).read_text(encoding="utf-8"))
        self.assertEqual(stamped, document)

    def test_kbuild_or_stage_provenance_drift_is_rejected(self) -> None:
        (self.root_dir / "kernel" / "Kbuild").write_bytes(self.original + b"drift\n")
        with self.assertRaisesRegex(HELPER.PinError, "post-SUSFS Kbuild changed"):
            self._pin()
        (self.root_dir / "kernel" / "Kbuild").write_bytes(self.original)
        (self.root_dir / HELPER.STAGE_STAMP).write_text(
            json.dumps({"variant": "kernelsu", "source_commit": "b" * 40}), encoding="utf-8"
        )
        with self.assertRaisesRegex(HELPER.PinError, "provenance"):
            self._pin()

    def test_next_variant_requires_exact_integration_stamp(self) -> None:
        (self.root_dir / HELPER.STAGE_STAMP).write_text(
            json.dumps({"variant": "kernelsu-next", "source_commit": self.commit}), encoding="utf-8"
        )
        with self.assertRaisesRegex(HELPER.PinError, "integration stamp"):
            self._pin("kernelsu-next")
        (self.root_dir / HELPER.INTEGRATION_STAMP).write_text(
            json.dumps({"integration": "changed"}), encoding="utf-8"
        )
        with self.assertRaisesRegex(HELPER.PinError, "provenance changed"):
            self._pin("kernelsu-next")

    def test_root_source_must_stay_inside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as outside_name:
            outside = Path(outside_name).resolve()
            with mock.patch.dict(HELPER.PINS, {"kernelsu": self.pin_spec}):
                with self.assertRaisesRegex(HELPER.PinError, "escapes"):
                    HELPER.pin(self.workspace, outside, "kernelsu")


if __name__ == "__main__":
    unittest.main()
