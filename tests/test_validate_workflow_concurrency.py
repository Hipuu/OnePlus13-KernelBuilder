from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ValidateWorkflowConcurrencyTests(unittest.TestCase):
    def test_manual_rehearsals_cannot_be_cancelled_by_push_validation(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "validate.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "group: validate-${{ github.event_name }}-"
            "${{ github.event.pull_request.number || github.ref }}",
            workflow,
        )
        self.assertIn(
            "cancel-in-progress: ${{ github.event_name != 'workflow_dispatch' }}",
            workflow,
        )


if __name__ == "__main__":
    unittest.main()
