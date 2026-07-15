from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIRECTORY = ROOT / ".github" / "workflows"
LOCAL_BUILD_CALL = "uses: ./.github/workflows/build.yml"


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
        for path in sorted(WORKFLOW_DIRECTORY.glob("*.yml")):
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


if __name__ == "__main__":
    unittest.main()
