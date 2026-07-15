from __future__ import annotations

import io
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lib.build import (
    _configure_common_gki_defconfig,
    _copy_declared_dist_module_payload,
    _external_module_commands,
    _build_epoch,
    _official_build_paths,
    _official_cache_path,
    _clean_official_output,
    _validate_official_module_payload_records,
    _verify_official_module_payload,
    MAX_BUILD_EPOCH,
    build_kernel,
)
from lib.config import discover_configs, sha256_file
from lib.context import advance_context, load_context, new_context, write_context
from lib.errors import BuildToolError
from tests.support import make_repository


class OfficialBuildContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = make_repository(Path(self.temporary.name))
        self.device, self.lock, self.profiles, self.features = discover_configs(self.root)
        self.profile = self.profiles["oos16"]
        self.source = self.root / "out" / "source"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_exact_sun_perf_paths_have_no_recursive_fallback(self) -> None:
        official_output, kernel_kit = _official_build_paths(self.source, self.device)
        self.assertEqual(
            official_output,
            self.source / "kernel_platform" / "out" / "msm-kernel-sun-perf",
        )
        self.assertEqual(kernel_kit, self.source / "device" / "qcom" / "sun-kernel")
        self.assertEqual(
            _official_cache_path(self.source, self.device),
            self.source / "kernel_platform" / "bazel-cache",
        )

    def test_clean_flag_is_the_only_way_to_delete_official_bazel_cache(self) -> None:
        official_output, kernel_kit = _official_build_paths(self.source, self.device)
        cache_dir = _official_cache_path(self.source, self.device)
        retained_host_output = self.source / "kernel_platform" / "out" / "host-cache"
        for directory in (official_output / "dist", kernel_kit, cache_dir, retained_host_output):
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "marker").write_text("fixture\n", encoding="utf-8")

        _clean_official_output(
            self.source,
            self.root / "out" / "build",
            self.device,
            clean=False,
        )
        self.assertTrue((cache_dir / "marker").is_file())
        self.assertTrue((retained_host_output / "marker").is_file())
        self.assertFalse((official_output / "dist").exists())
        self.assertFalse(kernel_kit.exists())

        _clean_official_output(
            self.source,
            self.root / "out" / "build",
            self.device,
            clean=True,
        )
        self.assertFalse(cache_dir.exists())
        self.assertFalse((self.source / "kernel_platform" / "out").exists())

    def test_official_cache_rejects_symlinked_ancestor_without_deleting_target(self) -> None:
        kernel_platform = self.source / "kernel_platform"
        real_parent = kernel_platform / "real-cache-parent"
        nested_target = real_parent / "nested"
        nested_target.mkdir(parents=True)
        marker = nested_target / "marker"
        marker.write_text("keep\n", encoding="utf-8")
        link = kernel_platform / "cache-link"
        try:
            link.symlink_to(real_parent, target_is_directory=True)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"directory symlinks are unavailable: {exc}")
        nested_device = replace(self.device, official_cache_dir="cache-link/nested")

        with self.assertRaisesRegex(BuildToolError, "symlinked official build cache component"):
            _clean_official_output(
                self.source,
                self.root / "out" / "build",
                nested_device,
                clean=True,
            )
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep\n")

    def test_external_module_plan_uses_only_pinned_helper(self) -> None:
        commands = _external_module_commands(
            self.source,
            self.root / "out" / "modules",
            self.device,
            ["rtw88"],
        )
        flattened = [part for command in commands for part in command]
        self.assertEqual(Path(commands[0][0]).name, "brunch")
        self.assertEqual(
            commands[1],
            [
                "bash",
                str(self.source / "kernel_platform" / "build" / "build_module.sh"),
            ],
        )
        self.assertNotIn("make", flattened)
        self.assertNotIn("modules_install", flattened)

    def test_official_module_payload_must_match_kleaf_declarations(self) -> None:
        dist = self.root / "out" / "preserved-dist"
        module = dist / "drivers" / "net" / "can" / "vcan.ko"
        module.parent.mkdir(parents=True)
        module.write_bytes(b"vcan fixture\n")
        (dist / "modules.order").write_text(
            "drivers/net/can/vcan.ko\n", encoding="utf-8"
        )
        ordered, verification = _verify_official_module_payload(
            dist,
            ["drivers/net/can/vcan.ko"],
        )
        self.assertEqual([path.as_posix() for path in ordered], ["drivers/net/can/vcan.ko"])
        self.assertEqual(verification["paths"], ["drivers/net/can/vcan.ko"])
        with self.assertRaisesRegex(BuildToolError, "missing"):
            _verify_official_module_payload(
                dist,
                ["drivers/net/can/vcan.ko", "drivers/net/can/slcan/slcan.ko"],
            )

    def test_modules_staging_archive_is_safely_and_exactly_selected(self) -> None:
        unsafe_dist = self.root / "out" / "unsafe-dist"
        unsafe_dist.mkdir(parents=True)
        with tarfile.open(unsafe_dist / "modules_staging_dir.tar.gz", "w:gz") as archive:
            payload = b"escape\n"
            member = tarfile.TarInfo("../escape.ko")
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
        with self.assertRaisesRegex(BuildToolError, "unsafe member"):
            _copy_declared_dist_module_payload(
                unsafe_dist,
                self.root / "out" / "unsafe-payload",
                ["drivers/net/can/vcan.ko"],
            )
        self.assertFalse((self.root / "out" / "escape.ko").exists())

        missing_dist = self.root / "out" / "missing-dist"
        missing_dist.mkdir(parents=True)
        with tarfile.open(missing_dist / "modules_staging_dir.tar.gz", "w:gz") as archive:
            payload = b"other\n"
            member = tarfile.TarInfo(
                "./lib/modules/6.6.0-fixture/kernel/drivers/net/can/other.ko"
            )
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
        with self.assertRaisesRegex(BuildToolError, "lacks declared output"):
            _copy_declared_dist_module_payload(
                missing_dist,
                self.root / "out" / "missing-payload",
                ["drivers/net/can/vcan.ko"],
            )

    def test_build_timestamp_rejects_values_outside_zip_range(self) -> None:
        self.assertEqual(
            _build_epoch(self.source, self.device.common_kernel, str(MAX_BUILD_EPOCH)),
            MAX_BUILD_EPOCH,
        )
        with self.assertRaisesRegex(BuildToolError, "2107-12-31"):
            _build_epoch(
                self.source,
                self.device.common_kernel,
                str(MAX_BUILD_EPOCH + 1),
            )

    def test_build_epoch_resolves_configured_timestamp_before_source_epoch(self) -> None:
        with patch.dict(
            os.environ,
            {
                "BUILD_TIMESTAMP": "2026-07-14T12:00:00Z",
                "SOURCE_DATE_EPOCH": "1",
            },
            clear=False,
        ):
            self.assertEqual(
                _build_epoch(self.source, self.device.common_kernel, None),
                1784030400,
            )
            self.assertEqual(
                _build_epoch(self.source, self.device.common_kernel, "123"),
                123,
            )

    def test_build_epoch_validates_source_date_epoch_as_an_integer(self) -> None:
        with patch.dict(
            os.environ,
            {"BUILD_TIMESTAMP": "", "SOURCE_DATE_EPOCH": "456"},
            clear=False,
        ):
            self.assertEqual(
                _build_epoch(self.source, self.device.common_kernel, " "),
                456,
            )
        with patch.dict(
            os.environ,
            {"BUILD_TIMESTAMP": "", "SOURCE_DATE_EPOCH": "not-an-epoch"},
            clear=False,
        ):
            with self.assertRaisesRegex(BuildToolError, "SOURCE_DATE_EPOCH.*epoch integer"):
                _build_epoch(self.source, self.device.common_kernel, None)

    def test_fake_config_patch_uses_only_the_resolved_source_epoch(self) -> None:
        fake_config_patch = (
            ROOT / "patches" / "common" / "0006-fake-config-oneplus-6.6.patch"
        ).read_text(encoding="utf-8")
        self.assertNotRegex(fake_config_patch, r"\$\(shell\s+date\b")
        self.assertIn(
            "SOURCE_DATE_EPOCH is required for reproducible fake-config builds",
            fake_config_patch,
        )
        self.assertIn(
            'CFLAGS_configs.o += -D__FORCE_REBUILD__="$(SOURCE_DATE_EPOCH)"',
            fake_config_patch,
        )

    def test_common_gki_defconfig_is_canonicalized_for_kleaf(self) -> None:
        common = self.source / self.device.common_kernel
        source_defconfig = common / "arch" / "arm64" / "configs" / "gki_defconfig"
        source_defconfig.parent.mkdir(parents=True)
        source_defconfig.write_text("CONFIG_TEST=y\n", encoding="utf-8")
        config_tool = common / "scripts" / "config"
        config_tool.parent.mkdir(parents=True)
        config_tool.write_text("#!/bin/sh\n", encoding="utf-8")
        metadata = self.root / "out" / "build" / ".op13"
        targets: list[str] = []

        def fake_run(argv, **_kwargs):
            target = str(argv[-1])
            targets.append(target)
            output_arg = next(str(value) for value in argv if str(value).startswith("O="))
            output = Path(output_arg[2:])
            if target == "savedefconfig":
                (output / "defconfig").write_bytes((output / ".config").read_bytes())
            elif target == "gki_defconfig":
                (output / ".config").write_bytes(source_defconfig.read_bytes())
            return subprocess.CompletedProcess(argv, 0, "", "")

        with patch("lib.build.CommandRunner") as runner_type:
            runner_type.return_value.run.side_effect = fake_run
            requested, consumed = _configure_common_gki_defconfig(
                source_dir=self.source,
                metadata_dir=metadata,
                device=self.device,
                fragments=[],
                forced={},
            )
        self.assertEqual(consumed, source_defconfig)
        self.assertEqual(requested.read_text(encoding="utf-8"), "CONFIG_TEST=y\n")
        self.assertEqual(
            targets,
            ["olddefconfig", "savedefconfig", "gki_defconfig", "olddefconfig"],
        )

    def test_kernel_build_forces_recompile_and_records_two_configs(self) -> None:
        resolved = self.source / ".op13" / "resolved.xml"
        resolved.parent.mkdir(parents=True)
        resolved.write_bytes(self.profile.locked_manifest.read_bytes())
        context = new_context(self.profile, self.lock, resolved, smoke=False)
        context = advance_context(
            context,
            "patches-applied",
            {"features": [{"profile": "test", "root_variant": "none"}]},
        )
        output = self.root / "out" / "build"
        output.mkdir(parents=True)
        requested_config = output / ".config"
        requested_config.write_text("CONFIG_TEST=y\n", encoding="utf-8")
        configuration = {
            "profile": self.profile.id,
            "feature_profile": "test",
            "root_variant": "none",
            "optimization": "O2",
            "lto": "thin",
            "build_target": "mixed",
            "required_symbols": {"CONFIG_TEST": "y"},
            "config_sha256": sha256_file(requested_config),
            "requested_config_sha256": sha256_file(requested_config),
            "module_outputs": {
                "requested_paths": ["drivers/net/can/vcan.ko"],
                "active_paths": [],
            },
        }
        context = advance_context(context, "configured", {"configuration": configuration})
        context_path = self.source / ".op13" / "build-context.json"
        write_context(context_path, context)
        official_script = self.source / self.device.official_script
        official_script.parent.mkdir(parents=True)
        official_script.write_text("#!/bin/sh\n", encoding="utf-8")
        captured_env: dict[str, str] = {}

        def fake_build(_argv, *, cwd, env, log_path):
            del cwd
            captured_env.update(env)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("exact build\n", encoding="utf-8")
            official_output, source_kit = _official_build_paths(self.source, self.device)
            dist = official_output / "dist"
            dist.mkdir(parents=True)
            source_kit.mkdir(parents=True)
            payloads = {
                "Image": b"I" * (1024 * 1024 + 1),
                "Module.symvers": b"symvers\n",
                "System.map": b"map\n",
                ".config": b"CONFIG_MODULES=y\n",
                "vmlinux": b"elf\n",
            }
            for name, payload in payloads.items():
                (dist / name).write_bytes(payload)
                (source_kit / name).write_bytes(payload)
            module_payload = b"fixture module\n"
            with tarfile.open(dist / "modules_staging_dir.tar.gz", "w:gz") as archive:
                member = tarfile.TarInfo(
                    "./lib/modules/6.6.0-fixture/kernel/drivers/net/can/vcan.ko"
                )
                member.size = len(module_payload)
                archive.addfile(member, io.BytesIO(module_payload))
                build_link = tarfile.TarInfo("./lib/modules/6.6.0-fixture/build")
                build_link.type = tarfile.SYMTYPE
                build_link.linkname = "/ignored/generated/source"
                archive.addfile(build_link)
            (source_kit / "build_opts.txt").write_text("--lto=thin\n", encoding="utf-8")

        def fake_extract(_common, _image, destination):
            destination.write_text("CONFIG_TEST=y\n", encoding="utf-8")
            return destination

        with (
            patch("lib.build._build_epoch", return_value=123),
            patch("lib.build._run_logged", side_effect=fake_build),
            patch("lib.build._extract_image_config", side_effect=fake_extract),
        ):
            build_kernel(
                source_dir=self.source,
                output_dir=output,
                context_path=context_path,
                profile=self.profile,
                device=self.device,
                lock=self.lock,
                clean=False,
                debug=False,
                smoke=False,
                dry_run=False,
                branding="ExactContract",
                build_timestamp=None,
            )
        self.assertEqual(captured_env["RECOMPILE_KERNEL"], "1")
        self.assertEqual(captured_env["COPY_NEEDED"], "1")
        self.assertEqual(captured_env["LTO"], "thin")
        self.assertEqual(captured_env["SOURCE_DATE_EPOCH"], "123")
        self.assertEqual(
            captured_env["KBUILD_BUILD_TIMESTAMP"],
            "Thu Jan 01 00:02:03 UTC 1970",
        )
        self.assertNotIn("KCONFIG_CONFIG", captured_env)
        self.assertNotIn("DIST_DIR", captured_env)
        self.assertEqual((output / ".config").read_text(encoding="utf-8"), "CONFIG_TEST=y\n")
        self.assertEqual(
            (output / "kernel-kit" / ".config").read_text(encoding="utf-8"),
            "CONFIG_MODULES=y\n",
        )
        built_context = load_context(context_path)
        kernel_record = built_context["kernel"]
        self.assertEqual(
            kernel_record["module_staging_archive"]["kernel_release"],
            "6.6.0-fixture",
        )
        order_record = kernel_record["official_modules_order"]
        module_records = kernel_record["official_modules"]
        preserved_dist = output / "kernel-dist-modules"
        _validate_official_module_payload_records(
            preserved_dist,
            order_record,
            module_records,
        )
        preserved_module = preserved_dist / "drivers" / "net" / "can" / "vcan.ko"
        preserved_module.write_bytes(b"changed module\n")
        with self.assertRaisesRegex(BuildToolError, "(?:size|digest) differs"):
            _validate_official_module_payload_records(
                preserved_dist,
                order_record,
                module_records,
            )
        preserved_module.write_bytes(b"fixture module\n")
        preserved_order = preserved_dist / "modules.order"
        preserved_order.write_text("drivers/net/can/other.ko\n", encoding="utf-8")
        with self.assertRaisesRegex(BuildToolError, "(?:size|digest) differs"):
            _validate_official_module_payload_records(
                preserved_dist,
                order_record,
                module_records,
            )


if __name__ == "__main__":
    unittest.main()
