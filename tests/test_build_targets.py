from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lib.build import (
    _compare_kernel_lineage,
    _fragment_paths,
    assert_build_target_contract,
    build_external_modules,
    build_kernel,
    configure_kernel,
)
from lib.config import KconfigFragment, discover_configs
from lib.context import load_context, new_context, write_context
from lib.errors import BuildToolError
from tests.support import make_repository


class BuildTargetContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = make_repository(Path(self.temporary.name))
        self.device, self.lock, self.profiles, self.features = discover_configs(self.root)
        self.profile = self.profiles["oos16"]
        self.feature = self.features["test"]
        self.source = self.root / "out" / "source"
        (self.source / ".op13").mkdir(parents=True)
        self.resolved = self.source / ".op13" / "resolved.xml"
        self.resolved.write_bytes(self.profile.locked_manifest.read_bytes())
        self.context_path = self.source / ".op13" / "build-context.json"
        self._reset_source_context()
        self.build = self.root / "out" / "build"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _reset_source_context(self) -> None:
        write_context(
            self.context_path,
            new_context(self.profile, self.lock, self.resolved, smoke=True),
        )

    def _configure(self, build_target: str) -> None:
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
            build_target=build_target,
            smoke=True,
            check_only=False,
        )

    def _build_kernel(self) -> None:
        build_kernel(
            source_dir=self.source,
            output_dir=self.build,
            context_path=self.context_path,
            profile=self.profile,
            device=self.device,
            lock=self.lock,
            clean=False,
            debug=False,
            smoke=True,
            dry_run=False,
            branding="TargetContract",
            build_timestamp=None,
        )

    def test_monolithic_is_a_fail_closed_reserved_selector(self) -> None:
        with self.assertRaisesRegex(BuildToolError, "mixed GKI pipeline"):
            assert_build_target_contract("monolithic")
        with self.assertRaisesRegex(BuildToolError, "mixed GKI pipeline"):
            self._configure("monolithic")

    def test_fragment_scope_matrix_is_distinct(self) -> None:
        module_fragment = self.root / "patches" / "common" / "module.config"
        module_fragment.write_text("CONFIG_TEST_MODULE=m\n", encoding="utf-8")
        feature = replace(
            self.feature,
            kconfig_fragments=(
                *self.feature.kconfig_fragments,
                KconfigFragment(
                    path="patches/common/module.config",
                    scope="modules",
                    required=True,
                ),
            ),
        )
        common_only = _fragment_paths(self.root, feature, "kernel")
        for target in ("modules", "mixed"):
            with self.subTest(target=target):
                self.assertEqual(len(_fragment_paths(self.root, feature, target)), 2)
        self.assertEqual(
            [path.resolve() for path in common_only],
            [(self.root / "patches" / "common" / "test.config").resolve()],
        )

    def test_kernel_phase_rejects_modules_only_configuration(self) -> None:
        self._configure("mixed")
        context = load_context(self.context_path)
        configuration = dict(context["configuration"])
        configuration["build_target"] = "modules"
        context["configuration"] = configuration
        write_context(self.context_path, context)
        with self.assertRaisesRegex(BuildToolError, "kernel compilation is not part"):
            self._build_kernel()

    def test_module_phase_rejects_kernel_only_configuration(self) -> None:
        self._configure("kernel")
        self._build_kernel()
        with self.assertRaisesRegex(BuildToolError, "module compilation is not part"):
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
                debug=False,
                smoke=True,
                dry_run=False,
            )

    def test_modules_only_requires_mixed_kernel_prerequisite(self) -> None:
        self._configure("kernel")
        self._build_kernel()
        self._reset_source_context()
        with self.assertRaisesRegex(BuildToolError, "mixed-target kernel artifact"):
            self._configure("modules")

    def test_modules_only_preserves_its_final_target_contract(self) -> None:
        self._configure("mixed")
        self._build_kernel()
        self._reset_source_context()
        self._configure("modules")
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
            debug=False,
            smoke=True,
            dry_run=False,
        )
        final_context = load_context(self.build / ".op13" / "build-context.json")
        self.assertEqual(final_context["configuration"]["build_target"], "modules")
        self.assertEqual(final_context["modules"]["build_target"], "modules")

    def test_lineage_pairing_allows_only_mixed_module_abi(self) -> None:
        module_outputs = {
            "locked_profile": "fixture",
            "changed": False,
            "active_symbols": [],
            "requested_paths": [],
            "official_paths": [],
            "active_paths": [],
            "requested_paths_sha256": "d" * 64,
            "active_paths_sha256": "e" * 64,
            "modules_bzl": {
                "pre_sha256": "f" * 64,
                "post_sha256": "f" * 64,
            },
            "build_bazel": {
                "pre_sha256": "1" * 64,
                "post_sha256": "1" * 64,
            },
            "msm_dist_bzl": {
                "pre_sha256": "2" * 64,
                "post_sha256": "3" * 64,
            },
        }
        configuration = {
            "profile": self.profile.id,
            "feature_profile": self.feature.id,
            "root_variant": "none",
            "optimization": "O2",
            "lto": "thin",
            "config_sha256": "a" * 64,
            "module_outputs": module_outputs,
        }
        base = {
            "profile": self.profile.id,
            "target": self.profile.target,
            "arch": self.profile.arch,
            "kmi": self.profile.kmi,
            "manifest": {
                "url": self.profile.manifest_url,
                "file": self.profile.manifest_file,
                "revision": self.profile.manifest_revision,
                "sha256": "b" * 64,
                "locked_sha256": "c" * 64,
            },
            "features": [{"profile": self.feature.id, "root_variant": "none"}],
        }
        kernel = {
            **base,
            "configuration": {**configuration, "build_target": "mixed"},
        }
        for source_target in ("modules", "mixed"):
            with self.subTest(source_target=source_target):
                source = {
                    **base,
                    "configuration": {**configuration, "build_target": source_target},
                }
                _compare_kernel_lineage(source, kernel)


if __name__ == "__main__":
    unittest.main()
