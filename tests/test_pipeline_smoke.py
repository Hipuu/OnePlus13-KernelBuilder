from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lib.artifacts import MAX_ZIP_EPOCH, deterministic_zip, package_build, verify_build_output
from lib.build import build_external_modules, build_kernel, configure_kernel, expected_symbols
from lib.config import discover_configs, sha256_file
from lib.context import load_context, new_context, write_context
from lib.errors import BuildToolError
from tests.support import make_repository


class SmokePipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = make_repository(Path(self.temporary.name))
        self.device, self.lock, self.profiles, self.features = discover_configs(self.root)
        self.profile = self.profiles["oos16"]
        self.feature = self.features["test"]
        self.source = self.root / "out" / "source"
        (self.source / ".op13").mkdir(parents=True)
        resolved = self.source / ".op13" / "resolved.xml"
        resolved.write_bytes(self.profile.locked_manifest.read_bytes())
        self.context_path = self.source / ".op13" / "build-context.json"
        write_context(self.context_path, new_context(self.profile, self.lock, resolved, smoke=True))
        self.build = self.root / "out" / "build"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_root_none_forces_all_ksu_assertions_off(self) -> None:
        expected = expected_symbols(
            self.feature,
            root_variant="none",
            optimization="O2",
            lto="thin",
        )
        self.assertEqual(expected["CONFIG_KSU"], "n")
        self.assertEqual(expected["CONFIG_MT76x0U"], "m")

    def test_complete_smoke_pipeline_with_modules_and_packages(self) -> None:
        configure_kernel(
            root=self.root,
            source_dir=self.source,
            output_dir=self.build,
            context_path=self.context_path,
            profile=self.profile,
            feature=self.feature,
            device=self.device,
            lock=self.lock,
            root_variant="none",
            optimization="O2",
            lto="thin",
            build_target="mixed",
            smoke=True,
            check_only=False,
        )
        build_kernel(
            source_dir=self.source,
            output_dir=self.build,
            context_path=self.context_path,
            profile=self.profile,
            device=self.device,
            lock=self.lock,
            clean=False,
            debug=True,
            smoke=True,
            dry_run=False,
            branding="SmokeTest",
            build_timestamp=None,
        )
        stale_module = self.build / "modules" / "staging" / "stale.ko"
        stale_module.parent.mkdir(parents=True)
        stale_module.write_bytes(b"stale\n")
        build_external_modules(
            source_dir=self.source,
            kernel_output=self.build,
            output_dir=self.build / "modules",
            source_context_path=self.context_path,
            profile=self.profile,
            feature=self.feature,
            device=self.device,
            lock=self.lock,
            cache_root=self.root / ".cache" / "op13",
            clean=False,
            debug=True,
            smoke=True,
            dry_run=False,
        )
        report = verify_build_output(
            output_dir=self.build,
            profile=self.profile,
            feature=self.feature,
            lock=self.lock,
            root_variant="none",
            build_target="mixed",
            smoke=True,
        )
        self.assertEqual(report["external_module_count"], 1)
        self.assertEqual(report["in_tree_module_count"], 1)
        self.assertFalse(stale_module.exists())
        build_context_path = self.build / ".op13" / "build-context.json"
        built_context = load_context(build_context_path)
        original_required = dict(built_context["configuration"]["required_symbols"])
        built_context["configuration"]["required_symbols"]["CONFIG_UNRECORDED_FINAL_GATE"] = "y"
        write_context(build_context_path, built_context)
        with self.assertRaisesRegex(
            BuildToolError,
            "CONFIG_UNRECORDED_FINAL_GATE: expected y, got n",
        ):
            verify_build_output(
                output_dir=self.build,
                profile=self.profile,
                feature=self.feature,
                lock=self.lock,
                root_variant="none",
                build_target="mixed",
                smoke=True,
            )
        built_context["configuration"]["required_symbols"] = original_required
        write_context(build_context_path, built_context)
        original_vermagic = built_context["modules"]["kernel_vermagic"]
        built_context["modules"]["kernel_vermagic"] = (
            "6.6.0-op13-smoke SMP mod_unload aarch64"
        )
        write_context(build_context_path, built_context)
        with self.assertRaisesRegex(
            BuildToolError,
            "in-tree module full vermagic differs",
        ):
            verify_build_output(
                output_dir=self.build,
                profile=self.profile,
                feature=self.feature,
                lock=self.lock,
                root_variant="none",
                build_target="mixed",
                smoke=True,
            )
        built_context["modules"]["kernel_vermagic"] = original_vermagic
        write_context(build_context_path, built_context)
        dist = self.root / "out" / "dist"
        records = package_build(
            root=self.root,
            input_dir=self.build,
            output_dir=dist,
            cache_root=self.root / ".cache" / "op13",
            profile=self.profile,
            feature=self.feature,
            lock=self.lock,
            root_variant="none",
            build_target="mixed",
            debug=True,
            pre_release=True,
            smoke=True,
        )
        roles = {record["role"] for record in records}
        self.assertTrue(
            {
                "kernel-image",
                "anykernel3-zip",
                "module-zip",
                "wireless-firmware",
                "debug-zip",
                "provenance",
                "checksums",
            }.issubset(roles)
        )
        firmware_record = next(record for record in records if record["role"] == "wireless-firmware")
        self.assertTrue(firmware_record["smoke_placeholder"])
        self.assertFalse(any(path.name.endswith(("boot.img", "vendor_boot.img")) for path in dist.iterdir()))
        self.assertTrue((dist / "BUILD-MANIFEST.json").is_file())
        manifest = json.loads((dist / "BUILD-MANIFEST.json").read_text(encoding="utf-8"))
        self.assertNotIn(str(self.root.resolve()), json.dumps(manifest))
        self.assertEqual(manifest["schema_version"], 2)
        self.assertEqual(manifest["builder"]["repository"], "Hipuu/OnePlus13-KernelBuilder")
        self.assertEqual(manifest["base"], "oos16")
        self.assertTrue(manifest["debug"])
        self.assertEqual(manifest["kmi"], "android15-6.6")
        self.assertEqual(manifest["source"]["locked_path"], "manifests/lockfiles/oos16.xml")
        self.assertEqual(
            manifest["source"]["resolved_path"],
            next(path.name for path in dist.glob("*-manifest.xml")),
        )
        self.assertEqual(manifest["dependency_lock"]["path"], "dependencies/lock.yml")
        self.assertEqual(
            manifest["dependency_lock"]["sha256"],
            sha256_file(self.root / "dependencies" / "lock.yml"),
        )
        self.assertEqual(manifest["dependency_lock"]["canonical_sha256"], self.lock.digest)
        dependency_ids = [record["id"] for record in manifest["dependencies"]]
        self.assertEqual(dependency_ids, sorted(dependency_ids))
        kernelsu_next = next(
            record for record in manifest["dependencies"] if record["id"] == "kernelsu_next"
        )
        self.assertEqual(kernelsu_next["source"]["commit"], "5" * 40)
        firmware = next(
            record
            for record in manifest["dependencies"]
            if record["id"] == "nethunter_wireless_firmware"
        )
        self.assertEqual(firmware["resource"]["sha256"], "a" * 64)
        self.assertTrue((dist / "SHA256SUMS").is_file())
        checksum_verification = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "verify-package-checksums.py"),
                "--directory",
                str(dist),
                "--context",
                str(build_context_path),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            checksum_verification.returncode,
            0,
            checksum_verification.stderr,
        )

        staging = self.build / "modules" / "staging"
        zip_calls = 0

        def inject_after_verification(
            source,
            destination,
            *,
            epoch,
            member_modes=None,
        ):
            nonlocal zip_calls
            zip_calls += 1
            deterministic_zip(
                source,
                destination,
                epoch=epoch,
                member_modes=member_modes,
            )
            if zip_calls == 1:
                (staging / "POST-VERIFY.txt").write_text(
                    "injected after entry verification\n",
                    encoding="utf-8",
                )

        with mock.patch(
            "lib.artifacts.deterministic_zip",
            side_effect=inject_after_verification,
        ):
            with self.assertRaisesRegex(BuildToolError, "ZIP contents differ"):
                package_build(
                    root=self.root,
                    input_dir=self.build,
                    output_dir=self.root / "out" / "toctou-dist",
                    cache_root=self.root / ".cache" / "op13",
                    profile=self.profile,
                    feature=self.feature,
                    lock=self.lock,
                    root_variant="none",
                    build_target="mixed",
                    debug=False,
                    pre_release=True,
                    smoke=True,
                )
        (staging / "POST-VERIFY.txt").unlink()

        unrecorded = staging / "UNRECORDED.txt"
        unrecorded.write_text("not in the staging manifest\n", encoding="utf-8")
        with self.assertRaisesRegex(BuildToolError, "unrecorded or changed packaged file"):
            verify_build_output(
                output_dir=self.build,
                profile=self.profile,
                feature=self.feature,
                lock=self.lock,
                root_variant="none",
                build_target="mixed",
                smoke=True,
            )
        unrecorded.unlink()

        built_context = load_context(build_context_path)
        external_record = built_context["modules"]["external_modules"][0]["modules"][0]
        external_module = staging / external_record["path"]
        moved_module = staging / "WRONG-SUBTREE" / external_module.name
        moved_module.parent.mkdir(parents=True)
        external_module.rename(moved_module)
        with self.assertRaisesRegex(BuildToolError, "recorded external module is missing"):
            verify_build_output(
                output_dir=self.build,
                profile=self.profile,
                feature=self.feature,
                lock=self.lock,
                root_variant="none",
                build_target="mixed",
                smoke=True,
            )
        moved_module.rename(external_module)

        built_context["modules"]["external_modules"] = []
        write_context(build_context_path, built_context)
        with self.assertRaisesRegex(BuildToolError, "records are missing"):
            verify_build_output(
                output_dir=self.build,
                profile=self.profile,
                feature=self.feature,
                lock=self.lock,
                root_variant="none",
                build_target="mixed",
                smoke=True,
            )

    def test_deterministic_zip_is_byte_reproducible(self) -> None:
        source = self.root / "zip-input"
        source.mkdir()
        (source / "b").write_text("two\n", encoding="utf-8")
        (source / "a").write_text("one\n", encoding="utf-8")
        first = self.root / "first.zip"
        second = self.root / "second.zip"
        deterministic_zip(source, first, epoch=0)
        deterministic_zip(source, second, epoch=0)
        self.assertEqual(sha256_file(first), sha256_file(second))
        with zipfile.ZipFile(first) as archive:
            self.assertEqual(archive.namelist(), ["a", "b"])
        with self.assertRaisesRegex(BuildToolError, "exceeds 2107"):
            deterministic_zip(source, self.root / "too-late.zip", epoch=MAX_ZIP_EPOCH + 1)


if __name__ == "__main__":
    unittest.main()
