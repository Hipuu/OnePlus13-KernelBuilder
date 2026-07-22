from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WorkflowLintContractTests(unittest.TestCase):
    def test_actionlint_is_fetched_from_the_lock_and_required(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "validate.yml").read_text(
            encoding="utf-8"
        )
        shell_start = workflow.index("      - name: Check shell and Python syntax\n")
        start = workflow.index(
            "      - name: Validate workflows with pinned Actionlint and ShellCheck\n"
        )
        end = workflow.index("\n      - name: Run unit tests\n", start)
        step = workflow[start:end]
        self.assertLess(shell_start, start)
        self.assertIn("--dependency actionlint_linux_amd64", step)
        self.assertIn("--dependency shellcheck_linux_x86_64", step)
        self.assertIn("scripts/run-pinned-actionlint.py", step)
        self.assertIn('--shellcheck-archive "$shellcheck_archive"', step)
        self.assertIn("-name '*.yml' -o -name '*.yaml'", step)
        self.assertNotIn("|| true", step)

    def test_shellcheck_is_fetched_from_the_lock_and_required(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "validate.yml").read_text(
            encoding="utf-8"
        )
        start = workflow.index("      - name: Check shell and Python syntax\n")
        end = workflow.index(
            "\n      - name: Validate workflows with pinned Actionlint and ShellCheck\n",
            start,
        )
        step = workflow[start:end]
        self.assertIn("--dependency shellcheck_linux_x86_64", step)
        self.assertEqual(step.count("scripts/run-pinned-shellcheck.py"), 2)
        self.assertIn('--archive "$shellcheck_archive" -- --version', step)
        self.assertIn("--external-sources --severity=warning", step)
        self.assertNotIn("command -v shellcheck", step)
        self.assertNotIn("shellcheck --external-sources", step)


if __name__ == "__main__":
    unittest.main()
