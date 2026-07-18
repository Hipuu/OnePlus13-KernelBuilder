from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lib.config import sha256_bytes
from lib.errors import BuildToolError
from lib import module_outputs


MODULES_SOURCE = """# SPDX-License-Identifier: GPL-2.0

_COMMON_GKI_MODULES_LIST = [
    "net/bluetooth/bluetooth.ko",
]

COMMON_GKI_MODULES_LIST = _COMMON_GKI_MODULES_LIST

_ARM_GKI_MODULES_LIST = [
    "drivers/example/arm.ko",
]
"""

BUILD_SOURCE = """load(
    ":modules.bzl",
    "get_gki_modules_list",
    "get_gki_protected_modules_list",
    "get_kunit_modules_list",
)

define_common_kernels(target_configs = {
    "kernel_aarch64": {
        "kmi_symbol_list_strict_mode": True,
        "protected_modules_list": ":gki_aarch64_protected_modules",
        "module_implicit_outs": get_gki_modules_list("arm64") + get_kunit_modules_list("arm64"),
        "make_goals": _GKI_AARCH64_MAKE_GOALS,
    },
    "kernel_aarch64_16k": {
        "module_implicit_outs": get_gki_modules_list("arm64") + get_kunit_modules_list("arm64"),
        "make_goals": _GKI_AARCH64_MAKE_GOALS,
    },
})
"""

MSM_DIST_SOURCE = """def _define_kernel_dist(base_kernel):
    kernel_modules_install(
        name = "{}_modules_install".format(target),
        kernel_build = ":{}".format(target),
    )

    dist_dir = "out/dist"

    msm_dist_targets = [base_kernel]

    copy_to_dist_dir(data = msm_dist_targets, dist_dir = dist_dir)
"""


class ModuleOutputIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.common = Path(self.temporary.name) / "kernel_platform" / "common"
        self.common.mkdir(parents=True)
        self.modules_path = self.common / "modules.bzl"
        self.build_path = self.common / "BUILD.bazel"
        self.modules_path.write_text(MODULES_SOURCE, encoding="utf-8", newline="\n")
        self.build_path.write_text(BUILD_SOURCE, encoding="utf-8", newline="\n")
        self.fixture_digest = sha256_bytes(MODULES_SOURCE.encode("utf-8"))
        self.fixture_build_digest = sha256_bytes(BUILD_SOURCE.encode("utf-8"))
        self.msm = Path(self.temporary.name) / "kernel_platform" / "msm-kernel"
        self.msm.mkdir(parents=True)
        self.msm_dist_path = self.msm / "msm_kernel_la.bzl"
        self.msm_dist_path.write_text(MSM_DIST_SOURCE, encoding="utf-8", newline="\n")
        self.fixture_msm_digest = sha256_bytes(MSM_DIST_SOURCE.encode("utf-8"))
        fixture_msm_post = MSM_DIST_SOURCE.replace(
            module_outputs._MSM_MODULES_INSTALL_ANCHOR,
            module_outputs._MSM_MODULES_INSTALL_REPLACEMENT,
            1,
        ).replace(
            module_outputs._MSM_DIST_ANCHOR,
            module_outputs._MSM_DIST_REPLACEMENT,
            1,
        )
        self.fixture_msm_post_digest = sha256_bytes(fixture_msm_post.encode("utf-8"))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _integrate(self, symbols, *, profile="oos15-global"):
        with mock.patch.object(
            module_outputs,
            "MODULES_BZL_PREIMAGE_SHA256",
            {self.fixture_digest: profile},
        ), mock.patch.object(
            module_outputs,
            "BUILD_BAZEL_PREIMAGE_SHA256",
            {self.fixture_build_digest: profile},
        ):
            return module_outputs.integrate_common_kleaf_module_outputs(self.common, symbols)

    def test_locked_modules_preimages_are_full_exact_digests(self) -> None:
        self.assertEqual(
            set(module_outputs.MODULES_BZL_PREIMAGE_SHA256),
            {
                "3b361b863d337b6d215ddca9747371025b241b72a1e6dce020b85875113c0f88",
                "df523fc074baae9496a1593147b8781e37e0260e73f465813d1cfe2125f724ba",
                "33f709345f2bee3470b1905098fdc789d64f2e97ab5b2f6cea8fad727b67d17a",
            },
        )
        self.assertEqual(
            set(module_outputs.BUILD_BAZEL_PREIMAGE_SHA256),
            {
                "f0c9c372a0e5f107dc07f18a800376a9165e8db9e58532da01ba4be237ce0456",
                "b3ddc9e0ecccb91b4e291001d4f12e8b207c43c782b9e214ad5936b531dd8bbd",
                "a04b08e432dbcc3c73869a922eb52dd410eeb7747862fa3f3321a4a0e68f97a6",
            },
        )
        self.assertEqual(
            set(module_outputs.MSM_DIST_BZL_PREIMAGE_PROFILES),
            {
                "e5f7bddde1f41266fd72b33b52fb3c7c5e5e4ba1979c945cdf6e6a042caaef58",
                "7faf46339672923d7ab8e12acb7a46e19ca8cab4a4adcc32ae92130ee15b2a9e",
            },
        )
        self.assertEqual(
            module_outputs.MSM_DIST_BZL_POSTIMAGE_BY_PREIMAGE,
            {
                "e5f7bddde1f41266fd72b33b52fb3c7c5e5e4ba1979c945cdf6e6a042caaef58":
                    "96fa43b81387873201c53a873644427db6d570a038e70b2967464386b0de3f4e",
                "7faf46339672923d7ab8e12acb7a46e19ca8cab4a4adcc32ae92130ee15b2a9e":
                    "a9be9148143b738b9210fd82d78de98a5f690af7c524d65b048c195fcc4889ea",
            },
        )

    def test_msm_dist_exports_the_final_mixed_module_archive(self) -> None:
        with mock.patch.object(
            module_outputs,
            "MSM_DIST_BZL_PREIMAGE_PROFILES",
            {self.fixture_msm_digest: frozenset({"fixture"})},
        ), mock.patch.object(
            module_outputs,
            "MSM_DIST_BZL_POSTIMAGE_BY_PREIMAGE",
            {self.fixture_msm_digest: self.fixture_msm_post_digest},
        ):
            record = module_outputs.integrate_msm_kleaf_module_dist(
                self.msm,
                expected_profile="fixture",
            )
        text = self.msm_dist_path.read_text(encoding="utf-8")
        self.assertTrue(record["changed"])
        self.assertEqual(record["locked_profile"], "fixture")
        self.assertEqual(text.count('output_group = "modules_staging_archive"'), 1)
        self.assertEqual(text.count('":{}_op13_modules_staging_archive".format(target)'), 1)
        self.assertIn("OP13_MODULE_STAGING_ARCHIVE:BEGIN", text)

    def test_msm_dist_rejects_profile_mix_and_modified_preimage(self) -> None:
        with mock.patch.object(
            module_outputs,
            "MSM_DIST_BZL_PREIMAGE_PROFILES",
            {self.fixture_msm_digest: frozenset({"fixture"})},
        ):
            with self.assertRaisesRegex(BuildToolError, "does not match common profile"):
                module_outputs.integrate_msm_kleaf_module_dist(
                    self.msm,
                    expected_profile="other",
                )
            self.msm_dist_path.write_text(MSM_DIST_SOURCE + "# changed\n", encoding="utf-8")
            with self.assertRaisesRegex(BuildToolError, "unrecognized or already modified"):
                module_outputs.integrate_msm_kleaf_module_dist(
                    self.msm,
                    expected_profile="fixture",
                )

    def test_exact_can_and_usb_serial_makefile_paths_are_allowlisted(self) -> None:
        expected = {
            "CONFIG_CAN_C_CAN": "drivers/net/can/c_can/c_can.ko",
            "CONFIG_CAN_CC770_PLATFORM": "drivers/net/can/cc770/cc770_platform.ko",
            "CONFIG_CAN_M_CAN_TCAN4X5X": "drivers/net/can/m_can/tcan4x5x.ko",
            "CONFIG_CAN_ESD_USB": "drivers/net/can/usb/esd_usb.ko",
            "CONFIG_CAN_KVASER_USB": "drivers/net/can/usb/kvaser_usb/kvaser_usb.ko",
            "CONFIG_CAN_PEAK_USB": "drivers/net/can/usb/peak_usb/peak_usb.ko",
            "CONFIG_USB_SERIAL_CH341": "drivers/usb/serial/ch341.ko",
            "CONFIG_USB_SERIAL_PL2303": "drivers/usb/serial/pl2303.ko",
        }
        for symbol, path in expected.items():
            self.assertEqual(module_outputs.MODULE_OUTPUT_BY_SYMBOL[symbol], path)
        self.assertNotIn("CONFIG_CAN_ESD_USB2", module_outputs.MODULE_OUTPUT_BY_SYMBOL)

    def test_sdr_tuner_kconfig_closure_declares_exact_multi_output_set(self) -> None:
        expected_stems = {
            "e4000",
            "fc0011",
            "fc0012",
            "fc0013",
            "fc2580",
            "it913x",
            "m88rs6000t",
            "max2165",
            "mc44s803",
            "msi001",
            "mt2060",
            "mt2063",
            "mt20xx",
            "mt2131",
            "mt2266",
            "mxl301rf",
            "mxl5005s",
            "mxl5007t",
            "qm1d1b0004",
            "qm1d1c0042",
            "qt1010",
            "r820t",
            "si2157",
            "tda18212",
            "tda18218",
            "tda18250",
            "tda18271",
            "tda827x",
            "tda8290",
            "tda9887",
            "tea5761",
            "tea5767",
            "tua9001",
            "tuner-simple",
            "tuner-types",
            "xc2028",
            "xc4000",
            "xc5000",
        }
        expected_paths = {
            f"drivers/media/tuners/{stem}.ko" for stem in expected_stems
        }
        mapping = module_outputs.MEDIA_TUNER_MODULE_OUTPUTS_BY_SYMBOL
        flattened = [path for paths in mapping.values() for path in paths]

        self.assertEqual(len(mapping), 37)
        self.assertEqual(len(flattened), 38)
        self.assertEqual(set(flattened), expected_paths)
        self.assertEqual(len(flattened), len(set(flattened)))
        self.assertEqual(
            mapping["CONFIG_MEDIA_TUNER_SIMPLE"],
            (
                "drivers/media/tuners/tuner-simple.ko",
                "drivers/media/tuners/tuner-types.ko",
            ),
        )
        for symbol, paths in mapping.items():
            with self.subTest(symbol=symbol):
                self.assertEqual(
                    module_outputs.module_output_paths_for_symbol(symbol), paths
                )
                self.assertEqual(
                    module_outputs.MODULE_OUTPUT_BY_SYMBOL[symbol], paths[0]
                )
                self.assertEqual(
                    module_outputs.MODULE_EXTRA_OUTPUTS_BY_SYMBOL.get(symbol, ()),
                    paths[1:],
                )

        record = module_outputs.resolve_module_outputs(mapping)
        self.assertEqual(record["active_symbols"], sorted(mapping))
        self.assertEqual(record["active_paths"], sorted(expected_paths))
        self.assertEqual(record["official_paths"], [])
        self.assertEqual(record["requested_paths"], sorted(expected_paths))
        self.assertTrue(
            expected_paths.issubset(set(module_outputs.mapped_module_output_paths()))
        )

    def test_payload_consumers_use_the_flattened_multi_output_allowlist(self) -> None:
        for relative in ("scripts/lib/build.py", "scripts/lib/artifacts.py"):
            with self.subTest(relative=relative):
                text = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn("mapped_module_output_paths()", text)
                self.assertNotIn("MODULE_OUTPUT_BY_SYMBOL.values()", text)

    def test_oos15_integration_is_sorted_audited_and_scoped_to_4k_target(self) -> None:
        symbols = [
            "CONFIG_USB_SERIAL_CH341",
            "CONFIG_BT",
            "CONFIG_CAN_KVASER_USB",
            "CONFIG_ATH10K_USB",
        ]
        record = self._integrate(symbols)
        expected_active = sorted(
            [
                "drivers/usb/serial/ch341.ko",
                "drivers/net/can/usb/kvaser_usb/kvaser_usb.ko",
                "drivers/net/wireless/ath/ath10k/ath10k_usb.ko",
            ]
        )
        self.assertTrue(record["changed"])
        self.assertEqual(record["locked_profile"], "oos15-global")
        self.assertEqual(record["extended_targets"], ["kernel_aarch64"])
        self.assertEqual(record["active_symbols"], sorted(symbols))
        self.assertEqual(record["active_paths"], expected_active)
        self.assertEqual(record["official_paths"], ["net/bluetooth/bluetooth.ko"])
        self.assertNotEqual(
            record["modules_bzl"]["pre_sha256"],
            record["modules_bzl"]["post_sha256"],
        )
        self.assertNotEqual(
            record["build_bazel"]["pre_sha256"],
            record["build_bazel"]["post_sha256"],
        )

        modules_text = self.modules_path.read_text(encoding="utf-8")
        constant_text = modules_text.split("OP13_MODULE_IMPLICIT_OUTS = [", 1)[1].split(
            "]\n# OP13_MODULE_IMPLICIT_OUTS:END",
            1,
        )[0]
        rendered_paths = [
            line.strip().strip('",')
            for line in constant_text.splitlines()
            if line.startswith('    "') and line.rstrip().endswith('.ko",')
        ]
        self.assertEqual(rendered_paths, expected_active)
        self.assertEqual(modules_text.count("OP13_MODULE_IMPLICIT_OUTS = ["), 1)

        build_text = self.build_path.read_text(encoding="utf-8")
        self.assertEqual(build_text.count('    "OP13_MODULE_IMPLICIT_OUTS",'), 1)
        self.assertEqual(build_text.count("            OP13_MODULE_IMPLICIT_OUTS"), 1)
        target_16k = build_text.split('    "kernel_aarch64_16k": {', 1)[1]
        self.assertNotIn("OP13_MODULE_IMPLICIT_OUTS", target_16k)
        self.assertIn(
            '"module_implicit_outs": get_gki_modules_list("arm64") + '
            'get_kunit_modules_list("arm64"),',
            target_16k,
        )

    def test_oos16_extends_4k_and_16k_companion_targets(self) -> None:
        record = self._integrate(["CONFIG_MEMKERNEL"], profile="oos16")

        self.assertTrue(record["changed"])
        self.assertEqual(record["locked_profile"], "oos16")
        self.assertEqual(
            record["extended_targets"],
            ["kernel_aarch64", "kernel_aarch64_16k"],
        )
        build_text = self.build_path.read_text(encoding="utf-8")
        self.assertEqual(build_text.count('    "OP13_MODULE_IMPLICIT_OUTS",'), 1)
        self.assertEqual(build_text.count("            OP13_MODULE_IMPLICIT_OUTS"), 2)
        for target_name in ("kernel_aarch64", "kernel_aarch64_16k"):
            target = build_text.split(f'    "{target_name}": {{', 1)[1].split("\n    },", 1)[0]
            self.assertIn("OP13_MODULE_IMPLICIT_OUTS", target)

    def test_oos16_missing_16k_companion_target_fails_before_writes(self) -> None:
        changed_build = BUILD_SOURCE.replace(
            '    "kernel_aarch64_16k": {\n'
            '        "module_implicit_outs": get_gki_modules_list("arm64") + '
            'get_kunit_modules_list("arm64"),\n'
            '        "make_goals": _GKI_AARCH64_MAKE_GOALS,\n'
            '    },\n',
            "",
        )
        self.build_path.write_text(changed_build, encoding="utf-8", newline="\n")
        changed_digest = sha256_bytes(changed_build.encode("utf-8"))
        modules_before = self.modules_path.read_bytes()
        build_before = self.build_path.read_bytes()
        with mock.patch.object(
            module_outputs,
            "MODULES_BZL_PREIMAGE_SHA256",
            {self.fixture_digest: "oos16"},
        ), mock.patch.object(
            module_outputs,
            "BUILD_BAZEL_PREIMAGE_SHA256",
            {changed_digest: "oos16"},
        ):
            with self.assertRaisesRegex(BuildToolError, "kernel_aarch64_16k target anchor"):
                module_outputs.integrate_common_kleaf_module_outputs(
                    self.common,
                    ["CONFIG_MEMKERNEL"],
                )
        self.assertEqual(self.modules_path.read_bytes(), modules_before)
        self.assertEqual(self.build_path.read_bytes(), build_before)

    def test_empty_and_official_only_sets_are_validated_noops(self) -> None:
        modules_before = self.modules_path.read_bytes()
        build_before = self.build_path.read_bytes()
        empty = self._integrate([])
        self.assertFalse(empty["changed"])
        self.assertEqual(empty["extended_targets"], [])
        self.assertEqual(empty["active_paths"], [])
        self.assertEqual(empty["modules_bzl"]["pre_sha256"], empty["modules_bzl"]["post_sha256"])
        self.assertEqual(empty["build_bazel"]["pre_sha256"], empty["build_bazel"]["post_sha256"])
        self.assertEqual(self.modules_path.read_bytes(), modules_before)
        self.assertEqual(self.build_path.read_bytes(), build_before)

        official = self._integrate(
            [
                "CONFIG_BT",
                "CONFIG_BT_BCM",
                "CONFIG_CAN",
                "CONFIG_CAN_DEV",
                "CONFIG_CAN_SLCAN",
                "CONFIG_CAN_VCAN",
                "CONFIG_USB_SERIAL",
                "CONFIG_USB_SERIAL_FTDI_SIO",
            ]
        )
        self.assertFalse(official["changed"])
        self.assertEqual(official["active_paths"], [])
        self.assertEqual(set(official["official_paths"]), module_outputs.OFFICIAL_GKI_MODULE_OUTPUTS)

    def test_unknown_invalid_and_repeated_symbols_fail_before_writes(self) -> None:
        modules_before = self.modules_path.read_bytes()
        build_before = self.build_path.read_bytes()
        cases = [
            ["CONFIG_NOT_ALLOWLISTED"],
            ["CONFIG_BT", "CONFIG_BT"],
            ["BT"],
            "CONFIG_BT",
        ]
        for symbols in cases:
            with self.subTest(symbols=symbols):
                with self.assertRaises(BuildToolError):
                    self._integrate(symbols)
                self.assertEqual(self.modules_path.read_bytes(), modules_before)
                self.assertEqual(self.build_path.read_bytes(), build_before)

    def test_unrecognized_modules_preimage_and_changed_build_anchor_fail_closed(self) -> None:
        self.modules_path.write_text(MODULES_SOURCE + "# tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(BuildToolError, "unrecognized or already modified"):
            self._integrate(["CONFIG_MEMKERNEL"])
        self.assertEqual(self.build_path.read_text(encoding="utf-8"), BUILD_SOURCE)

        self.modules_path.write_text(MODULES_SOURCE, encoding="utf-8", newline="\n")
        changed_build = BUILD_SOURCE.replace('    "kernel_aarch64": {', '    "kernel_arm64": {')
        self.build_path.write_text(changed_build, encoding="utf-8", newline="\n")
        with self.assertRaisesRegex(BuildToolError, "unrecognized or already modified BUILD.bazel"):
            self._integrate(["CONFIG_MEMKERNEL"])
        self.assertEqual(self.modules_path.read_text(encoding="utf-8"), MODULES_SOURCE)

    def test_repeated_integration_is_rejected(self) -> None:
        self._integrate(["CONFIG_MEMKERNEL"])
        modules_after_first = self.modules_path.read_bytes()
        build_after_first = self.build_path.read_bytes()
        with self.assertRaisesRegex(BuildToolError, "unrecognized or already modified"):
            self._integrate(["CONFIG_MEMKERNEL"])
        self.assertEqual(self.modules_path.read_bytes(), modules_after_first)
        self.assertEqual(self.build_path.read_bytes(), build_after_first)

    def test_unsafe_allowlisted_path_is_rejected(self) -> None:
        with mock.patch.object(
            module_outputs,
            "MODULE_OUTPUT_BY_SYMBOL",
            {"CONFIG_TEST": "../escape.ko"},
        ):
            with self.assertRaisesRegex(BuildToolError, "unsafe module output path"):
                module_outputs.resolve_module_outputs(["CONFIG_TEST"])

    def test_multi_output_constant_validation_rejects_unknown_and_duplicate_paths(self) -> None:
        with mock.patch.object(
            module_outputs,
            "MEDIA_TUNER_MODULE_OUTPUTS_BY_SYMBOL",
            {},
        ), mock.patch.object(
            module_outputs,
            "MODULE_OUTPUT_BY_SYMBOL",
            {"CONFIG_TEST": "drivers/example/primary.ko"},
        ), mock.patch.object(
            module_outputs,
            "MODULE_EXTRA_OUTPUTS_BY_SYMBOL",
            {"CONFIG_UNKNOWN": ("drivers/example/extra.ko",)},
        ):
            with self.assertRaisesRegex(RuntimeError, "unknown symbols"):
                module_outputs._validate_module_output_constants()

        with mock.patch.object(
            module_outputs,
            "MEDIA_TUNER_MODULE_OUTPUTS_BY_SYMBOL",
            {},
        ), mock.patch.object(
            module_outputs,
            "MODULE_OUTPUT_BY_SYMBOL",
            {"CONFIG_TEST": "drivers/example/repeated.ko"},
        ), mock.patch.object(
            module_outputs,
            "MODULE_EXTRA_OUTPUTS_BY_SYMBOL",
            {"CONFIG_TEST": ("drivers/example/repeated.ko",)},
        ), mock.patch.object(
            module_outputs,
            "OFFICIAL_GKI_MODULE_OUTPUTS",
            frozenset(),
        ):
            with self.assertRaisesRegex(RuntimeError, "repeated .ko paths"):
                module_outputs._validate_module_output_constants()

        with mock.patch.object(
            module_outputs,
            "MODULE_OUTPUT_BY_SYMBOL",
            {"CONFIG_TEST": "drivers/example/safe.ko"},
        ), mock.patch.object(
            module_outputs,
            "MODULE_EXTRA_OUTPUTS_BY_SYMBOL",
            {"CONFIG_TEST": ("../escape.ko",)},
        ):
            with self.assertRaisesRegex(BuildToolError, "unsafe module output path"):
                module_outputs.resolve_module_outputs(["CONFIG_TEST"])

    def test_produced_path_verifier_requires_exact_safe_unique_set(self) -> None:
        declared = ["drivers/example/z.ko", "drivers/example/a.ko"]
        record = module_outputs.verify_produced_module_outputs(
            declared,
            reversed(declared),
        )
        self.assertEqual(record["count"], 2)
        self.assertEqual(record["paths"], sorted(declared))
        self.assertEqual(record["declared_paths_sha256"], record["produced_paths_sha256"])

        with self.assertRaisesRegex(BuildToolError, "missing: drivers/example/z.ko"):
            module_outputs.verify_produced_module_outputs(declared, ["drivers/example/a.ko"])
        with self.assertRaisesRegex(BuildToolError, "unexpected: drivers/example/b.ko"):
            module_outputs.verify_produced_module_outputs(
                ["drivers/example/a.ko"],
                ["drivers/example/a.ko", "drivers/example/b.ko"],
            )
        with self.assertRaisesRegex(BuildToolError, "unsafe module output path"):
            module_outputs.verify_produced_module_outputs(["../escape.ko"], [])
        with self.assertRaisesRegex(BuildToolError, "repeated module output path"):
            module_outputs.verify_produced_module_outputs(
                ["drivers/example/a.ko", "drivers/example/a.ko"],
                [],
            )


if __name__ == "__main__":
    unittest.main()
