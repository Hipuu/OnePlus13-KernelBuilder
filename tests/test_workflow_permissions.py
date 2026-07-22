from __future__ import annotations

import os
import re
import shutil
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIRECTORY = ROOT / ".github" / "workflows"
LOCAL_BUILD_CALL = "uses: ./.github/workflows/build.yml"
ALL_PERMISSION_SCOPES = {
    "actions",
    "attestations",
    "checks",
    "contents",
    "deployments",
    "discussions",
    "id-token",
    "issues",
    "models",
    "packages",
    "pages",
    "pull-requests",
    "security-events",
    "statuses",
}


def _workflow_paths() -> list[Path]:
    return sorted(
        path
        for path in WORKFLOW_DIRECTORY.iterdir()
        if path.is_file() and path.suffix in {".yml", ".yaml"}
    )


def _job_blocks(path: Path) -> list[tuple[str, list[str]]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    try:
        jobs_index = lines.index("jobs:")
    except ValueError as exc:
        raise AssertionError(f"{path}: missing top-level jobs mapping") from exc

    result: list[tuple[str, list[str]]] = []
    current_name: str | None = None
    current_lines: list[str] = []
    for line in lines[jobs_index + 1 :]:
        if line and not line.startswith(" "):
            break
        if line.startswith("  ") and not line.startswith("    ") and line.endswith(":"):
            if current_name is not None:
                result.append((current_name, current_lines))
            current_name = line.strip()[:-1]
            current_lines = []
        elif current_name is not None:
            current_lines.append(line)
    if current_name is not None:
        result.append((current_name, current_lines))
    return result


class WorkflowPermissionContractTests(unittest.TestCase):
    def test_local_build_callers_grant_actions_read(self) -> None:
        callers: list[str] = []
        for path in _workflow_paths():
            for job_name, lines in _job_blocks(path):
                if not any(line.strip() == LOCAL_BUILD_CALL for line in lines):
                    continue
                callers.append(f"{path.name}:{job_name}")
                permissions_index = next(
                    (
                        index
                        for index, line in enumerate(lines)
                        if line == "    permissions:"
                    ),
                    None,
                )
                self.assertIsNotNone(
                    permissions_index,
                    f"{path.name}:{job_name} must declare caller permissions",
                )
                permission_lines: list[str] = []
                for line in lines[permissions_index + 1 :]:
                    if line and not line.startswith("      "):
                        break
                    permission_lines.append(line.strip())
                self.assertIn(
                    "actions: read",
                    permission_lines,
                    f"{path.name}:{job_name} cannot call build.yml without actions: read",
                )
                self.assertIn("contents: read", permission_lines)

        self.assertEqual(
            callers,
            [
                "nightly.yml:nightly-build",
                "release.yml:module-kernel-prerequisite",
                "release.yml:rebuild",
                "validate.yml:oos16-full-build",
            ],
        )

    def test_write_permissions_have_exact_workflow_and_job_owners(self) -> None:
        writes: list[tuple[str, str, str]] = []
        for path in _workflow_paths():
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(
                re.search(
                    r"^[ \t]*permissions:[ \t]*(?:read-all|write-all)[ \t]*$",
                    text,
                    re.MULTILINE,
                ),
                f"{path.name}: broad permission shorthand is forbidden",
            )
            for permission in re.findall(
                r"^[ \t]+([a-z][a-z-]*):[ \t]+write[ \t]*$",
                text,
                re.MULTILINE,
            ):
                self.assertIn(
                    permission,
                    ALL_PERMISSION_SCOPES,
                    f"{path.name}: unknown permission scope",
                )
                owner = "workflow"
                for job_name, lines in _job_blocks(path):
                    block = "\n".join(lines)
                    if re.search(
                        rf"^[ \t]+{re.escape(permission)}:[ \t]+write[ \t]*$",
                        block,
                        re.MULTILINE,
                    ):
                        owner = job_name
                        break
                writes.append((path.name, owner, permission))
        self.assertEqual(
            writes,
            [
                ("cleanup.yml", "workflow", "actions"),
                ("release.yml", "publish", "attestations"),
                ("release.yml", "publish", "contents"),
                ("release.yml", "publish", "id-token"),
                ("source-monitor.yml", "workflow", "issues"),
            ],
        )

    def test_release_publish_is_the_only_environment_gated_writer(self) -> None:
        release = WORKFLOW_DIRECTORY / "release.yml"
        jobs = dict(_job_blocks(release))
        publish = "\n".join(jobs["publish"])
        self.assertIn("    environment:", publish)
        self.assertIn("      name: release", publish)
        self.assertIn("      contents: write", publish)
        for job_name, lines in jobs.items():
            if job_name == "publish":
                continue
            block = "\n".join(lines)
            self.assertIsNone(
                re.search(
                    r"^[ \t]+\S+:[ \t]+write[ \t]*$",
                    block,
                    re.MULTILINE,
                )
            )

    def test_inline_permission_detection_never_crosses_a_line_boundary(self) -> None:
        pattern = re.compile(
            r"^[ \t]*permissions:[ \t]*([^ \t\r\n].*)$",
            re.MULTILINE,
        )
        self.assertEqual(pattern.findall("permissions:\n  contents: read\n"), [])
        self.assertEqual(pattern.findall("permissions: {}\n"), ["{}"])
        self.assertEqual(pattern.findall("permissions: write-all\n"), ["write-all"])
        for path in _workflow_paths():
            values = pattern.findall(path.read_text(encoding="utf-8"))
            self.assertTrue(
                all(value.strip() == "{}" for value in values),
                f"{path.name}: inline permission mappings are forbidden",
            )

    def test_embedded_workflow_policy_accepts_the_checked_in_workflows(self) -> None:
        validate = (WORKFLOW_DIRECTORY / "validate.yml").read_text(encoding="utf-8")
        step = validate.split(
            "      - name: Enforce workflow pinning and credential policy\n",
            1,
        )[1].split("\n  patch-rehearsal:\n", 1)[0]
        embedded = step.split("          python3 - <<'PY'\n", 1)[1].split(
            "\n          PY",
            1,
        )[0]
        script = textwrap.dedent(embedded)
        with tempfile.TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            destination = root / ".github" / "workflows"
            destination.mkdir(parents=True)
            for path in _workflow_paths():
                shutil.copy2(path, destination / path.name)
            previous = Path.cwd()
            try:
                os.chdir(root)
                exec(compile(script, "workflow-permission-policy", "exec"), {})
            finally:
                os.chdir(previous)

    def test_every_checkout_disables_persisted_credentials(self) -> None:
        checkout_count = 0
        for path in _workflow_paths():
            lines = path.read_text(encoding="utf-8").splitlines()
            for index, line in enumerate(lines):
                if not re.search(r"uses:\s+actions/checkout@[0-9a-f]{40}", line):
                    continue
                checkout_count += 1
                following = "\n".join(lines[index + 1 : index + 8])
                self.assertIn(
                    "persist-credentials: false",
                    following,
                    f"{path.name}:{index + 1}",
                )
        self.assertGreater(checkout_count, 0)

    def test_every_external_action_uses_a_full_commit(self) -> None:
        for path in _workflow_paths():
            for line_number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1
            ):
                match = re.match(r"^\s*(?:-\s*)?uses:\s*([^\s#]+)", line)
                if match is None or match.group(1).startswith("./"):
                    continue
                self.assertRegex(
                    match.group(1),
                    r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*@[0-9a-f]{40}$",
                    f"{path.name}:{line_number}",
                )


if __name__ == "__main__":
    unittest.main()
