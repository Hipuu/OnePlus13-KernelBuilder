from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DebugArtifactScopeTests(unittest.TestCase):
    def test_upload_excludes_only_disposable_config_source_trees(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "build.yml").read_text(
            encoding="utf-8"
        )
        start = workflow.index("      - name: Upload debug evidence\n")
        step = workflow[start:]
        path_start = step.index("          path: |\n")
        path_end = step.index("          if-no-files-found:", path_start)
        path_lines = [
            line.strip()
            for line in step[path_start:path_end].splitlines()[1:]
            if line.strip()
        ]
        exclusions = [line for line in path_lines if line.startswith("!")]
        self.assertEqual(
            exclusions,
            [
                "!out/build/.op13/config-work/**",
                "!out/build/.op13/config-work-msm-kernel/**",
            ],
        )
        for retained in (
            "out/debug",
            "out/source/.op13",
            "out/build/.op13",
            "out/build/.config",
            "out/build/vmlinux",
            "out/build/System.map",
            "out/build/Module.symvers",
            "out/build/modules",
        ):
            with self.subTest(retained=retained):
                self.assertIn(retained, path_lines)

        # These records remain under the positive out/build/.op13 selection;
        # neither exact negative prefix can match them.
        retained_metadata = (
            "out/build/.op13/build-context.json",
            "out/build/.op13/config-request.json",
            "out/build/.op13/configuration-context.json",
            "out/build/.op13/kernel-build.log",
            "out/build/.op13/resolved-manifest.xml",
        )
        for path in retained_metadata:
            with self.subTest(path=path):
                self.assertFalse(
                    path.startswith("out/build/.op13/config-work/")
                )
                self.assertFalse(
                    path.startswith(
                        "out/build/.op13/config-work-msm-kernel/"
                    )
                )

    def test_upload_keeps_hidden_diagnostics_enabled(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "build.yml").read_text(
            encoding="utf-8"
        )
        start = workflow.index("      - name: Upload debug evidence\n")
        step = workflow[start:]
        self.assertIn("          include-hidden-files: true\n", step)

    def test_reusable_kernel_archive_uses_hardened_exclusion_contract(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "build.yml").read_text(
            encoding="utf-8"
        )
        start = workflow.index("      - name: Archive reusable kernel output\n")
        end = workflow.index("\n      - name: Upload reusable kernel output", start)
        step = workflow[start:end]
        self.assertIn("python3 scripts/kernel-artifact-archive.py create", step)
        self.assertIn('--source-dir "$BUILD_DIR"', step)
        self.assertIn(
            '--manifest "$KERNEL_ARTIFACT_DIR/KERNEL-ARTIFACT-MANIFEST.json"',
            step,
        )
        self.assertNotIn("tar --zstd", step)

        archive_helper = (
            ROOT / "scripts" / "kernel-artifact-archive.py"
        ).read_text(encoding="utf-8")
        contract_start = archive_helper.index("EXCLUDED_PREFIXES = (")
        contract_end = archive_helper.index("\n)", contract_start)
        exclusion_contract = archive_helper[contract_start:contract_end]
        self.assertIn('    "modules",', exclusion_contract)
        self.assertIn('    ".op13/config-work",', exclusion_contract)
        self.assertIn(
            '    ".op13/config-work-msm-kernel",',
            exclusion_contract,
        )
        for retained in (
            ".op13/build-context.json",
            ".op13/build-toolchain-provenance.json",
            ".op13/kmi-symbol-exports.json",
            ".op13/kmi-wireless-led-exports.json",
            ".op13/resolved-manifest.xml",
        ):
            with self.subTest(retained=retained):
                self.assertFalse(retained.startswith(".op13/config-work/"))
                self.assertFalse(
                    retained.startswith(".op13/config-work-msm-kernel/")
                )

    def test_kmi_stamps_are_captured_before_configuration_and_build(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "build.yml").read_text(
            encoding="utf-8"
        )
        start = workflow.index("      - name: Capture validated KMI patch evidence\n")
        end = workflow.index("\n      - name: Configure kernel and modules", start)
        step = workflow[start:end]
        self.assertIn("python3 scripts/capture-kmi-build-evidence.py", step)
        self.assertIn('--context "$SOURCE_DIR/.op13/build-context.json"', step)
        self.assertIn('--output "$SOURCE_DIR/.op13"', step)
        self.assertIn('--output "$DEBUG_DIR"', step)
        self.assertLess(start, workflow.index("      - name: Build kernel\n"))


if __name__ == "__main__":
    unittest.main()
