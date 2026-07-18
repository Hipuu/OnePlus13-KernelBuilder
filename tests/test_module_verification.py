from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lib.build import (
    _assert_full_vermagic,
    _module_vermagic,
    _run_depmod_verification,
)
from lib.artifacts import _verify_depmod_proof
from lib.config import sha256_file
from lib.errors import BuildToolError


class ModuleVerificationTests(unittest.TestCase):
    def test_module_vermagic_preserves_full_value_and_release(self) -> None:
        runner = mock.Mock()
        runner.run.return_value = subprocess.CompletedProcess(
            ["modinfo"],
            0,
            "6.6.0-op13 SMP preempt mod_unload aarch64\n",
            "",
        )
        module = Path("fixture.ko")

        vermagic, release = _module_vermagic(runner, module)

        self.assertEqual(vermagic, "6.6.0-op13 SMP preempt mod_unload aarch64")
        self.assertEqual(release, "6.6.0-op13")
        runner.run.assert_called_once_with(
            ["modinfo", "-F", "vermagic", str(module)],
            capture=True,
        )

    def test_module_vermagic_rejects_invalid_values(self) -> None:
        for output in ("", " \n", "6.6.0-op13 SMP\nsecond record\n"):
            with self.subTest(output=output):
                runner = mock.Mock()
                runner.run.return_value = subprocess.CompletedProcess(
                    ["modinfo"],
                    0,
                    output,
                    "",
                )
                with self.assertRaisesRegex(BuildToolError, "invalid module vermagic"):
                    _module_vermagic(runner, Path("fixture.ko"))

    def test_full_vermagic_mismatch_is_rejected_even_when_release_matches(self) -> None:
        with self.assertRaisesRegex(BuildToolError, "full module vermagic mismatch"):
            _assert_full_vermagic(
                "6.6.0-op13 SMP preempt mod_unload aarch64",
                "6.6.0-op13 SMP mod_unload aarch64",
                Path("fixture.ko"),
            )

    def test_depmod_success_returns_structured_proof_and_logs_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            system_map = root / "System.map"
            system_map.write_text("00000000 T fixture\n", encoding="utf-8")
            staging = root / "staging"
            staging.mkdir()
            log_path = root / "modules-build.log"
            runner = mock.Mock()
            runner.run.return_value = subprocess.CompletedProcess(
                ["depmod"],
                0,
                "depmod stdout\n",
                "depmod stderr\n",
            )

            proof = _run_depmod_verification(
                runner,
                system_map=system_map,
                staging=staging,
                kernel_release="6.6.0-op13",
                log_path=log_path,
            )

            expected_argv = [
                "depmod",
                "-e",
                "-F",
                str(system_map),
                "-b",
                str(staging),
                "6.6.0-op13",
            ]
            self.assertEqual(proof["status"], "passed")
            self.assertEqual(proof["argv"], expected_argv)
            self.assertEqual(proof["returncode"], 0)
            self.assertEqual(proof["kernel_release"], "6.6.0-op13")
            self.assertEqual(proof["system_map_sha256"], sha256_file(system_map))
            output = b"depmod stdout\ndepmod stderr\n"
            self.assertEqual(proof["output_sha256"], hashlib.sha256(output).hexdigest())
            self.assertEqual(proof["output_size"], len(output))
            runner.run.assert_called_once_with(
                expected_argv,
                capture=True,
                check=False,
            )
            log = log_path.read_text(encoding="utf-8")
            self.assertIn("+ depmod -e -F", log)
            self.assertTrue(log.endswith(output.decode("utf-8")))

    def test_depmod_unknown_symbol_is_rejected_at_zero_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            system_map = root / "System.map"
            system_map.write_text("00000000 T fixture\n", encoding="utf-8")
            staging = root / "staging"
            staging.mkdir()
            runner = mock.Mock()
            runner.run.return_value = subprocess.CompletedProcess(
                ["depmod"],
                0,
                "",
                "depmod: WARNING: fixture.ko needs unknown symbol missing_symbol\n",
            )

            with self.assertRaisesRegex(BuildToolError, "found unresolved symbols"):
                _run_depmod_verification(
                    runner,
                    system_map=system_map,
                    staging=staging,
                    kernel_release="6.6.0-op13",
                )

    def test_depmod_nonzero_status_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            system_map = root / "System.map"
            system_map.write_text("00000000 T fixture\n", encoding="utf-8")
            staging = root / "staging"
            staging.mkdir()
            runner = mock.Mock()
            runner.run.return_value = subprocess.CompletedProcess(
                ["depmod"],
                1,
                "",
                "depmod failed to parse fixture.ko\n",
            )

            with self.assertRaisesRegex(
                BuildToolError,
                r"unresolved-symbol validation failed \(1\)",
            ):
                _run_depmod_verification(
                    runner,
                    system_map=system_map,
                    staging=staging,
                    kernel_release="6.6.0-op13",
                )

    def test_artifact_gate_revalidates_depmod_proof_paths_and_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            output = Path(temporary_name)
            system_map = output / "System.map"
            system_map.write_text("00000000 T fixture\n", encoding="utf-8")
            staging = output / "modules" / "staging"
            staging.mkdir(parents=True)
            runner = mock.Mock()
            runner.run.return_value = subprocess.CompletedProcess(
                ["depmod"],
                0,
                "",
                "",
            )
            proof = _run_depmod_verification(
                runner,
                system_map=system_map,
                staging=staging,
                kernel_release="6.6.0-op13",
            )

            _verify_depmod_proof(
                proof,
                output_dir=output,
                kernel_release="6.6.0-op13",
                system_map_sha256=sha256_file(system_map),
                smoke=False,
            )

            tampered = dict(proof)
            tampered["argv"] = [*proof["argv"]]
            tampered["argv"][5] = str(output / "other-staging")
            with self.assertRaisesRegex(BuildToolError, "different staging tree"):
                _verify_depmod_proof(
                    tampered,
                    output_dir=output,
                    kernel_release="6.6.0-op13",
                    system_map_sha256=sha256_file(system_map),
                    smoke=False,
                )

    def test_artifact_gate_rejects_unsuccessful_or_wrong_depmod_proof(self) -> None:
        base = {
            "schema_version": 1,
            "status": "passed",
            "argv": [
                "depmod",
                "-e",
                "-F",
                "System.map",
                "-b",
                "modules/staging",
                "6.6.0-op13",
            ],
            "returncode": 0,
            "kernel_release": "6.6.0-op13",
            "system_map_sha256": "a" * 64,
            "output_sha256": "b" * 64,
            "output_size": 0,
        }
        cases = (
            ({"status": "failed"}, "successful result"),
            ({"returncode": True}, "successful result"),
            ({"kernel_release": "6.6.0-other"}, "kernel release"),
            ({"system_map_sha256": "c" * 64}, "System.map"),
            ({"output_sha256": "invalid"}, "output evidence"),
        )
        for update, message in cases:
            with self.subTest(update=update):
                proof = {**base, **update}
                with self.assertRaisesRegex(BuildToolError, message):
                    _verify_depmod_proof(
                        proof,
                        output_dir=Path("."),
                        kernel_release="6.6.0-op13",
                        system_map_sha256="a" * 64,
                        smoke=False,
                    )


if __name__ == "__main__":
    unittest.main()
