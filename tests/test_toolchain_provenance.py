from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lib.errors import BuildToolError
from lib.toolchain_provenance import (
    BUILD_TOOLS_PROJECT_PATH,
    CLANG_PROJECT_PATH,
    HOST_ENVIRONMENT_TOOLS,
    KERNEL_BUILD_TOOLS_PROJECT_PATH,
    OFFICIAL_TOOL_VARIABLES,
    _environment_tool_records,
    _git_tree_file_binding,
    _github_runner_image_record,
    _normalize_version_output,
    _parse_manifest,
    inspect_build_toolchain,
    record_build_toolchain,
)


COMMON_COMMIT = "a" * 40
CLANG_COMMIT = "b" * 40
ROOT_PROJECT_COMMIT = "c" * 40
BUILD_TOOLS_COMMIT = "d" * 40
KERNEL_BUILD_TOOLS_COMMIT = "e" * 40


class ToolchainFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.source = root / "source"
        self.source.mkdir()
        self.manifest = self.source / ".op13" / "oos16-manifest-resolved.xml"
        self.manifest.parent.mkdir()
        self.common = self.source / "kernel_platform" / "common"
        self.common.mkdir(parents=True)
        self.constants = self.common / "build.config.constants"
        self.constants.write_text("CLANG_VERSION=rTEST\n", encoding="utf-8")
        self.clang_project = self.source.joinpath(*CLANG_PROJECT_PATH.split("/"))
        self.bin = self.clang_project / "clang-rTEST" / "bin"
        self.bin.mkdir(parents=True)
        (self.clang_project / "LICENSE").write_text(
            "fixture license\n", encoding="utf-8"
        )
        (self.clang_project / "BUILD.bazel").write_text(
            "# fixture build metadata\n", encoding="utf-8"
        )
        for name in OFFICIAL_TOOL_VARIABLES:
            prefix = b"#!/bin/sh\n" if name == "clang++" else b"\x7fELF"
            (self.bin / name).write_bytes(prefix + name.encode("ascii") + b"\n")

        (self.source / "LICENSE").write_text(
            "fixture root license\n", encoding="utf-8"
        )
        (self.source / "BUILD.bazel").write_text(
            "# fixture root build metadata\n", encoding="utf-8"
        )
        kleaf = self.source / "kernel_platform" / "build" / "kernel" / "kleaf"
        kleaf.mkdir(parents=True)
        (kleaf / "bazel.sh").write_text(
            "#!/bin/bash -e\n"
            "my_dir=$(dirname \"$(readlink -f \"$0\")\")\n"
            "original_sh=$my_dir/bazel.origin.sh\n"
            "\"$original_sh\" \"$@\"\n",
            encoding="utf-8",
        )
        (kleaf / "bazel.origin.sh").write_text(
            "#!/bin/bash -e\n"
            "KLEAF_REPO_DIR=$($(dirname $(dirname "
            "$(readlink -f \"$0\")))/gettop.sh)\n"
            "exec \"$KLEAF_REPO_DIR\"/prebuilts/build-tools/path/"
            "linux-x86/python3 $(dirname $(readlink -f \"$0\"))/"
            "bazel.py \"$KLEAF_REPO_DIR\" \"$@\"\n",
            encoding="utf-8",
        )
        (kleaf / "bazel.py").write_text(
            "#!/usr/bin/env python3\n"
            "_BAZEL_REL_PATH = "
            "\"prebuilts/kernel-build-tools/bazel/linux-x86_64/bazel\"\n",
            encoding="utf-8",
        )
        (kleaf.parent / "gettop.sh").write_text(
            "#!/bin/bash\npwd -P\n",
            encoding="utf-8",
        )
        tools = self.source / "kernel_platform" / "tools"
        tools.mkdir(parents=True)
        bazel_wrapper = kleaf / "bazel.sh"
        if os.name == "nt":
            (tools / "bazel").symlink_to(bazel_wrapper.resolve())
        else:
            (tools / "bazel").symlink_to("../build/kernel/kleaf/bazel.sh")

        self.build_tools = (
            self.source / "kernel_platform" / "prebuilts" / "build-tools"
        )
        bazel_python = self.build_tools / "path" / "linux-x86" / "python3"
        bazel_python.parent.mkdir(parents=True)
        bazel_python.write_bytes(b"\x7fELFfixture python\n")
        (self.build_tools / "LICENSE").write_text(
            "fixture build-tools license\n", encoding="utf-8"
        )
        (self.build_tools / "BUILD.bazel").write_text(
            "# fixture build-tools metadata\n", encoding="utf-8"
        )

        self.kernel_build_tools = (
            self.source
            / "kernel_platform"
            / "prebuilts"
            / "kernel-build-tools"
        )
        bazel_binary = (
            self.kernel_build_tools / "bazel" / "linux-x86_64" / "bazel"
        )
        bazel_binary.parent.mkdir(parents=True)
        bazel_binary.write_bytes(b"\x7fELFfixture bazel\n")
        (self.kernel_build_tools / "LICENSE").write_text(
            "fixture kernel build-tools license\n", encoding="utf-8"
        )
        (self.kernel_build_tools / "BUILD.bazel").write_text(
            "# fixture kernel build-tools metadata\n", encoding="utf-8"
        )
        self.write_manifest()

    def write_manifest(
        self,
        *,
        common_commit: str = COMMON_COMMIT,
        clang_commit: str = CLANG_COMMIT,
        include_common: bool = True,
        include_clang: bool = True,
        clang_path: str = CLANG_PROJECT_PATH,
        include_root: bool = True,
        include_build_tools: bool = True,
        include_kernel_build_tools: bool = True,
    ) -> None:
        projects: list[str] = []
        if include_root:
            projects.append(
                '<project name="kernel/modules-and-devicetree" path="./" '
                f'revision="{ROOT_PROJECT_COMMIT}" />'
            )
        if include_common:
            projects.append(
                '<project name="kernel/common" '
                'path="kernel_platform/common" '
                f'revision="{common_commit}" />'
            )
        if include_clang:
            projects.append(
                '<project name="kernelplatform/prebuilts-master/clang/host/linux-x86" '
                f'path="{clang_path}" revision="{clang_commit}" />'
            )
        if include_build_tools:
            projects.append(
                '<project name="kernel_platform/prebuilts/build-tools" '
                'path="kernel_platform/prebuilts/build-tools" '
                f'revision="{BUILD_TOOLS_COMMIT}" />'
            )
        if include_kernel_build_tools:
            projects.append(
                '<project name="kernel/prebuilts/build-tools" '
                'path="kernel_platform/prebuilts/kernel-build-tools" '
                f'revision="{KERNEL_BUILD_TOOLS_COMMIT}" />'
            )
        self.manifest.write_text(
            "<manifest>\n  "
            + "\n  ".join(projects)
            + "\n</manifest>\n",
            encoding="utf-8",
        )


class ToolchainProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.fixture = ToolchainFixture(Path(self.temporary.name))
        self.readlink_patch = None
        if os.name == "nt":
            self.readlink_patch = patch(
                "lib.toolchain_provenance.os.readlink",
                return_value="../build/kernel/kleaf/bazel.sh",
            )
            self.readlink_patch.start()
        self.host_records = [
            {
                "name": "python3",
                "provenance": "environment-provided",
                "immutable": False,
                "canonical_path": "/runner/python3",
                "status": "available",
                "version_output": "Python 3.TEST",
            }
        ]
        self.git_binding_patch = patch(
            "lib.toolchain_provenance._git_tree_file_binding",
            side_effect=self._git_binding,
        )
        self.git_binding_patch.start()

    def tearDown(self) -> None:
        self.git_binding_patch.stop()
        if self.readlink_patch is not None:
            self.readlink_patch.stop()
        self.temporary.cleanup()

    def _git_binding(
        self,
        path: Path,
        *,
        source_root: Path,
        project: dict[str, str],
        **_: object,
    ) -> dict[str, object]:
        project_root = (
            source_root
            if project["path"] == "."
            else source_root.joinpath(*project["path"].split("/"))
        )
        relative = path.relative_to(project_root).as_posix()
        mode = "120000" if path.is_symlink() else "100644"
        object_id = "f" * 40
        return {
            "project_path": project["path"],
            "manifest_commit": project["commit"],
            "checkout_head": project["commit"],
            "path": relative,
            "tree_mode": mode,
            "tree_type": "blob",
            "tree_object": object_id,
            "worktree_object": object_id,
            "status": "exact-manifest-tree",
        }

    def _version_probe(
        self,
        command: list[str],
        **_: object,
    ) -> tuple[str, None]:
        return (
            f"{Path(command[0]).name} fixture version\n"
            "InstalledDir: ${SOURCE_ROOT}/toolchain",
            None,
        )

    def test_records_exact_official_tools_and_manifest_bindings(self) -> None:
        first = Path(self.temporary.name) / "debug" / "toolchain.json"
        second = self.fixture.source / ".op13" / "toolchain.json"
        with (
            patch(
                "lib.toolchain_provenance._environment_tool_records",
                return_value=self.host_records,
            ),
            patch(
                "lib.toolchain_provenance._run_version",
                side_effect=self._version_probe,
            ),
        ):
            document = record_build_toolchain(
                self.fixture.source,
                self.fixture.manifest,
                [first, second],
            )
            repeated = record_build_toolchain(
                self.fixture.source,
                self.fixture.manifest,
                [first, second],
            )

        self.assertEqual(document, repeated)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertEqual(json.loads(first.read_text(encoding="utf-8")), document)
        self.assertNotIn("created_at", document)
        self.assertNotIn("timestamp", document)
        self.assertEqual(document["schema_version"], 2)
        self.assertEqual(
            [item["name"] for item in document["compiler_tools"]],
            sorted(OFFICIAL_TOOL_VARIABLES),
        )
        self.assertEqual(document["selection"]["clang_version"], "rTEST")
        self.assertEqual(
            document["selection"]["manifest_project"],
            {
                "name": "kernelplatform/prebuilts-master/clang/host/linux-x86",
                "path": CLANG_PROJECT_PATH,
                "commit": CLANG_COMMIT,
            },
        )
        tools = {item["name"]: item for item in document["compiler_tools"]}
        self.assertEqual(tools["clang"]["official_variables"], ["AS", "CC", "HOSTCC"])
        self.assertEqual(tools["llvm-size"]["official_variables"], ["OBJSIZE"])
        self.assertEqual(tools["clang"]["kind"], "elf")
        self.assertEqual(tools["clang++"]["kind"], "script")
        for tool in tools.values():
            self.assertEqual(tool["manifest_project"]["commit"], CLANG_COMMIT)
            self.assertTrue(tool["sha256"])
            self.assertGreater(tool["size"], 0)
            self.assertNotIn(str(self.fixture.source), tool["version_output"])
            self.assertIn("${SOURCE_ROOT}", tool["version_output"])
            self.assertEqual(
                tool["selected_git_tree"]["status"],
                "exact-manifest-tree",
            )
            self.assertEqual(
                tool["canonical_git_tree"]["manifest_commit"],
                CLANG_COMMIT,
            )
            self.assertEqual(
                tool["nearest_metadata"]["license_or_notice"][
                    "ancestor_distance"
                ],
                2,
            )
            self.assertEqual(
                [
                    item["path"]
                    for item in tool["nearest_metadata"]["license_or_notice"][
                        "files"
                    ]
                ],
                [f"{CLANG_PROJECT_PATH}/LICENSE"],
            )
        bazel_components = document["bazel_launcher"]["components"]
        self.assertEqual(
            [item["role"] for item in bazel_components],
            [
                "entrypoint-and-oneplus-wrapper",
                "upstream-launcher",
                "repository-discovery-helper",
                "launcher-python-interpreter",
                "bazel-python-driver",
                "bazel-binary",
            ],
        )
        self.assertEqual(
            bazel_components[0]["selected_path"],
            "kernel_platform/tools/bazel",
        )
        self.assertEqual(
            bazel_components[0]["canonical_path"],
            "kernel_platform/build/kernel/kleaf/bazel.sh",
        )
        self.assertEqual(
            bazel_components[0]["symlink_target"],
            "../build/kernel/kleaf/bazel.sh",
        )
        self.assertEqual(
            bazel_components[3]["manifest_project"]["commit"],
            BUILD_TOOLS_COMMIT,
        )
        self.assertEqual(
            bazel_components[-1]["manifest_project"]["commit"],
            KERNEL_BUILD_TOOLS_COMMIT,
        )
        self.assertEqual(bazel_components[-1]["version_probe"], "--version")
        self.assertTrue(bazel_components[-1]["sha256"])
        self.assertEqual(document["host_environment_tools"], self.host_records)
        self.assertFalse(document["github_runner_image"]["immutable"])

    def test_missing_core_tool_fails_before_version_probes(self) -> None:
        (self.fixture.bin / "llvm-size").unlink()
        with (
            patch("lib.toolchain_provenance._run_version") as version,
            self.assertRaisesRegex(
                BuildToolError,
                "locked Clang toolchain is incomplete: llvm-size",
            ),
        ):
            inspect_build_toolchain(self.fixture.source, self.fixture.manifest)
        version.assert_not_called()

    def test_tool_resolving_outside_source_tree_is_rejected(self) -> None:
        selected = self.fixture.bin / "clang"
        selected.unlink()
        outside = Path(self.temporary.name) / "outside-clang"
        outside.write_bytes(b"\x7fELFoutside\n")
        try:
            selected.symlink_to(outside)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"file symlinks are unavailable: {exc}")
        with self.assertRaisesRegex(BuildToolError, "outside the synced source tree"):
            inspect_build_toolchain(self.fixture.source, self.fixture.manifest)

    def test_tool_must_bind_to_exact_clang_manifest_project(self) -> None:
        self.fixture.write_manifest(
            clang_path="kernel_platform/prebuilts/clang"
        )
        with (
            patch(
                "lib.toolchain_provenance._run_version",
                side_effect=self._version_probe,
            ),
            self.assertRaisesRegex(
                BuildToolError,
                "expected kernel_platform/prebuilts/clang/host/linux-x86",
            ),
        ):
            inspect_build_toolchain(self.fixture.source, self.fixture.manifest)

    def test_bazel_binary_is_required_and_manifest_bound(self) -> None:
        (
            self.fixture.kernel_build_tools
            / "bazel"
            / "linux-x86_64"
            / "bazel"
        ).unlink()
        with (
            patch(
                "lib.toolchain_provenance._run_version",
                side_effect=self._version_probe,
            ),
            self.assertRaisesRegex(BuildToolError, "pinned Bazel binary is missing"),
        ):
            inspect_build_toolchain(self.fixture.source, self.fixture.manifest)

        bazel_binary = (
            self.fixture.kernel_build_tools
            / "bazel"
            / "linux-x86_64"
            / "bazel"
        )
        bazel_binary.write_bytes(b"\x7fELFfixture bazel\n")
        self.fixture.write_manifest(include_kernel_build_tools=False)
        with (
            patch(
                "lib.toolchain_provenance._run_version",
                side_effect=self._version_probe,
            ),
            self.assertRaisesRegex(
                BuildToolError,
                "expected kernel_platform/prebuilts/kernel-build-tools",
            ),
        ):
            inspect_build_toolchain(self.fixture.source, self.fixture.manifest)

    def test_bazel_launcher_chain_must_retain_audited_targets(self) -> None:
        driver = (
            self.fixture.source
            / "kernel_platform"
            / "build"
            / "kernel"
            / "kleaf"
            / "bazel.py"
        )
        driver.write_text(
            '_BAZEL_REL_PATH = "prebuilts/other/bazel"\n',
            encoding="utf-8",
        )
        with (
            patch(
                "lib.toolchain_provenance._run_version",
                side_effect=self._version_probe,
            ),
            self.assertRaisesRegex(
                BuildToolError,
                "Bazel Python driver must select exactly",
            ),
        ):
            inspect_build_toolchain(self.fixture.source, self.fixture.manifest)

    def test_version_declaration_must_bind_to_common_project(self) -> None:
        self.fixture.write_manifest(include_common=False)
        with self.assertRaisesRegex(
            BuildToolError,
            "Clang version declaration.*expected kernel_platform/common",
        ):
            inspect_build_toolchain(self.fixture.source, self.fixture.manifest)

    def test_every_manifest_project_revision_must_be_immutable(self) -> None:
        self.fixture.write_manifest(clang_commit="dev")
        with self.assertRaisesRegex(
            BuildToolError,
            "not pinned to a lowercase 40-character commit",
        ):
            inspect_build_toolchain(self.fixture.source, self.fixture.manifest)

    def test_clang_version_declaration_is_unique(self) -> None:
        self.fixture.constants.write_text(
            "CLANG_VERSION=rTEST\nCLANG_VERSION=rOTHER\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            BuildToolError,
            "exactly one CLANG_VERSION",
        ):
            inspect_build_toolchain(self.fixture.source, self.fixture.manifest)

    def test_core_tool_version_probe_failure_is_fatal(self) -> None:
        result = SimpleNamespace(returncode=1, stdout="unsupported fixture\n")
        with (
            patch("lib.toolchain_provenance.subprocess.run", return_value=result),
            self.assertRaisesRegex(BuildToolError, "version probe.*exited 1"),
        ):
            inspect_build_toolchain(self.fixture.source, self.fixture.manifest)

    def test_manifest_itself_must_be_inside_synced_source(self) -> None:
        outside = Path(self.temporary.name) / "outside-manifest.xml"
        outside.write_bytes(self.fixture.manifest.read_bytes())
        with self.assertRaisesRegex(
            BuildToolError,
            "inside the synced source tree",
        ):
            inspect_build_toolchain(self.fixture.source, outside)

    def test_version_output_normalizes_source_path_and_line_endings(self) -> None:
        output = _normalize_version_output(
            f"clang\r\nInstalledDir: {self.fixture.source.resolve()}\r\n",
            self.fixture.source.resolve(),
        )
        self.assertEqual(output, "clang\nInstalledDir: ${SOURCE_ROOT}")

    def test_host_tools_are_explicitly_mutable_environment_inputs(self) -> None:
        root = Path(self.temporary.name)
        paths = {
            name: root / f"host-{name.replace('+', 'x')}"
            for name in HOST_ENVIRONMENT_TOOLS
        }
        paths.update(
            {
            "python3": root / "host-python",
            }
        )
        for path in paths.values():
            path.write_bytes(b"host fixture\n")

        def which(name: str) -> str:
            return str(paths[name])

        with (
            patch("lib.toolchain_provenance.shutil.which", side_effect=which),
            patch("lib.toolchain_provenance.sys.executable", str(paths["python3"])),
            patch(
                "lib.toolchain_provenance._run_version",
                return_value=("fixture version", None),
            ),
        ):
            records = _environment_tool_records(self.fixture.source.resolve())

        self.assertEqual(
            [item["name"] for item in records],
            sorted((*HOST_ENVIRONMENT_TOOLS, "python3")),
        )
        for record in records:
            self.assertEqual(record["provenance"], "environment-provided")
            self.assertFalse(record["immutable"])
            self.assertNotIn("sha256", record)
            self.assertNotIn("size", record)

    def test_github_runner_image_is_mutable_environment_evidence(self) -> None:
        with patch.dict(
            os.environ,
            {"ImageOS": "ubuntu24", "ImageVersion": "20260720.1"},
            clear=False,
        ):
            record = _github_runner_image_record()
        self.assertEqual(record["status"], "recorded")
        self.assertEqual(record["image_os"], "ubuntu24")
        self.assertEqual(record["image_version"], "20260720.1")
        self.assertEqual(record["provenance"], "github-actions-environment")
        self.assertFalse(record["immutable"])


@unittest.skipUnless(shutil.which("git"), "Git is required for tree-binding tests")
class GitTreeBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.source = Path(self.temporary.name) / "source"
        self.source.mkdir()
        self._git("init", "--quiet")
        self._git("config", "user.name", "OnePlus fixture")
        self._git("config", "user.email", "fixture@example.invalid")
        self.tool = self.source / "tool"
        self.tool.write_bytes(b"#!/bin/sh\necho fixture\n")
        if os.name != "nt":
            self.tool.chmod(0o755)
        self.other = self.source / "other-tool"
        self.other.write_bytes(b"#!/bin/sh\necho other\n")
        self.link = self.source / "tool-link"
        self.link_available = True
        try:
            self.link.symlink_to("tool")
        except (NotImplementedError, OSError):
            self.link_available = False
        self._git("add", "--all")
        self._git("commit", "--quiet", "-m", "fixture")
        self.commit = self._git("rev-parse", "HEAD").strip()
        self.project = {
            "name": "fixture/project",
            "path": ".",
            "commit": self.commit,
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _git(self, *arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.source), *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            self.fail(result.stderr)
        return result.stdout

    def _binding(self, path: Path) -> dict[str, object]:
        return _git_tree_file_binding(
            path,
            source_root=self.source.resolve(),
            project=self.project,
            label=path.name,
            project_cache={},
            file_cache={},
        )

    def test_clean_regular_file_is_bound_to_exact_commit_blob(self) -> None:
        binding = self._binding(self.tool)
        self.assertEqual(binding["manifest_commit"], self.commit)
        self.assertEqual(binding["checkout_head"], self.commit)
        self.assertEqual(binding["tree_object"], binding["worktree_object"])
        self.assertEqual(binding["status"], "exact-manifest-tree")

    def test_dirty_regular_file_content_is_rejected(self) -> None:
        self.tool.write_bytes(b"#!/bin/sh\necho modified\n")
        with self.assertRaisesRegex(
            BuildToolError,
            "worktree content differs from resolved-manifest commit",
        ):
            self._binding(self.tool)

    @unittest.skipIf(os.name == "nt", "POSIX executable mode is unavailable")
    def test_dirty_executable_mode_is_rejected(self) -> None:
        current = stat.S_IMODE(self.tool.stat().st_mode)
        self.tool.chmod(current & ~0o111)
        with self.assertRaisesRegex(BuildToolError, "worktree mode"):
            self._binding(self.tool)

    def test_dirty_symbolic_link_target_is_rejected(self) -> None:
        if not self.link_available:
            self.skipTest("symbolic links are unavailable")
        self.link.unlink()
        self.link.symlink_to("other-tool")
        with self.assertRaisesRegex(
            BuildToolError,
            "worktree content differs from resolved-manifest commit",
        ):
            self._binding(self.link)


class ToolchainWorkflowContractTests(unittest.TestCase):
    @staticmethod
    def _text(relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_build_gate_runs_after_sync_and_before_patch_or_build(self) -> None:
        workflow = self._text(".github/workflows/build.yml")
        sync = workflow.index("- name: Synchronize locked sources")
        gate = workflow.index("- name: Verify and record pinned build toolchain")
        patch_step = workflow.index("- name: Apply selected patch series")
        build_step = workflow.index("- name: Build kernel")
        self.assertLess(sync, gate)
        self.assertLess(gate, patch_step)
        self.assertLess(gate, build_step)
        self.assertEqual(workflow.count("scripts/record-build-toolchain.py"), 1)
        self.assertIn(
            '--resolved-manifest "$SOURCE_DIR/.op13/${BASE}-manifest-resolved.xml"',
            workflow,
        )
        self.assertIn(
            '--output "$DEBUG_DIR/build-toolchain-provenance.json"',
            workflow,
        )
        self.assertIn(
            '--output "$SOURCE_DIR/.op13/build-toolchain-provenance.json"',
            workflow,
        )

    def test_patch_rehearsal_uses_same_pre_patch_gate(self) -> None:
        workflow = self._text(".github/workflows/validate.yml")
        start = workflow.index("  patch-rehearsal:\n")
        end = workflow.index("\n  oos16-full-build:\n", start)
        rehearsal = workflow[start:end]
        sync = rehearsal.index("- name: Synchronize locked sources")
        gate = rehearsal.index("- name: Verify and record pinned build toolchain")
        patch_step = rehearsal.index(
            "- name: Apply full patch series on disposable checkout"
        )
        self.assertLess(sync, gate)
        self.assertLess(gate, patch_step)
        self.assertEqual(rehearsal.count("scripts/record-build-toolchain.py"), 1)
        self.assertIn(
            '--output out/debug/build-toolchain-provenance.json',
            rehearsal,
        )
        self.assertIn(
            '--output out/source/.op13/build-toolchain-provenance.json',
            rehearsal,
        )

    def test_all_locked_manifests_bind_recorded_toolchain_projects(self) -> None:
        required = {
            ".",
            "kernel_platform/common",
            CLANG_PROJECT_PATH,
            BUILD_TOOLS_PROJECT_PATH,
            KERNEL_BUILD_TOOLS_PROJECT_PATH,
        }
        for base in ("oos15-cn", "oos15-global", "oos16"):
            with self.subTest(base=base):
                projects = _parse_manifest(
                    ROOT / "manifests" / "lockfiles" / f"{base}.xml"
                )
                self.assertTrue(
                    required.issubset(
                        {project["path"] for project in projects}
                    )
                )


if __name__ == "__main__":
    unittest.main()
