from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "integrate-classic-kernelsu-susfs.py"
SPEC = importlib.util.spec_from_file_location(
    "integrate_classic_kernelsu_susfs",
    SCRIPT,
)
assert SPEC is not None and SPEC.loader is not None
HELPER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = HELPER
SPEC.loader.exec_module(HELPER)


class ClassicKernelSuSusfsCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.ksu = Path(self.temporary.name) / "KernelSU"
        target = self.ksu / HELPER.TARGET
        target.parent.mkdir(parents=True)
        self.preimage = b"head\n" + b"middle\n".join(
            old for old, _ in HELPER.REPLACEMENTS
        ) + b"tail\n"
        self.postimage = HELPER._transform(self.preimage)
        target.write_bytes(self.preimage)
        (self.ksu / HELPER.STAGE_STAMP).write_text(
            json.dumps(
                {
                    "variant": "kernelsu",
                    "source_commit": HELPER.EXPECTED_KSU_COMMIT,
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _integrate(self) -> dict[str, object]:
        with (
            patch.object(HELPER, "PRE_SIZE", len(self.preimage)),
            patch.object(
                HELPER,
                "PRE_SHA256",
                hashlib.sha256(self.preimage).hexdigest(),
            ),
            patch.object(HELPER, "POST_SIZE", len(self.postimage)),
            patch.object(
                HELPER,
                "POST_SHA256",
                hashlib.sha256(self.postimage).hexdigest(),
            ),
        ):
            return HELPER.integrate(self.ksu)

    def test_exact_guards_become_direct_calls_and_are_idempotent(self) -> None:
        first = self._integrate()
        self.assertEqual(first["status"], "integrated")
        self.assertEqual((self.ksu / HELPER.TARGET).read_bytes(), self.postimage)
        second = self._integrate()
        self.assertEqual(second["status"], "already-integrated")
        stamp = json.loads(
            (self.ksu / HELPER.OUTPUT_STAMP).read_text(encoding="utf-8")
        )
        self.assertEqual(stamp, second)

    def test_source_or_stage_drift_fails_closed(self) -> None:
        (self.ksu / HELPER.TARGET).write_bytes(self.preimage + b"drift\n")
        with self.assertRaisesRegex(HELPER.IntegrationError, "pre/postimage changed"):
            self._integrate()
        (self.ksu / HELPER.TARGET).write_bytes(self.preimage)
        (self.ksu / HELPER.STAGE_STAMP).write_text(
            json.dumps({"variant": "kernelsu", "source_commit": "0" * 40}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(HELPER.IntegrationError, "stage provenance"):
            self._integrate()

    def test_checked_in_contract_matches_observed_nightly_preimage(self) -> None:
        self.assertEqual(HELPER.PRE_SIZE, 24646)
        self.assertEqual(
            HELPER.PRE_SHA256,
            "cb482202cf784394e7d8c2f1cc8b04dc1aa74396927e896e49b5b5c80d070b8c",
        )
        self.assertEqual(HELPER.POST_SIZE, 24459)
        self.assertEqual(
            HELPER.POST_SHA256,
            "c53265a24599570dd07af2f25b3dc7120f722ba5f70f9a30a91fbbecb16ad78a",
        )


if __name__ == "__main__":
    unittest.main()
