from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class HostedSourceSyncObservabilityTests(unittest.TestCase):
    @staticmethod
    def _text(relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def _patch_rehearsal_job(self) -> str:
        workflow = self._text(".github/workflows/validate.yml")
        start = workflow.index("  patch-rehearsal:\n")
        end = workflow.index("\n  oos16-full-build:\n", start)
        return workflow[start:end]

    def test_hosted_wrapper_bounds_checkout_and_preserves_pipeline_status(self) -> None:
        script = self._text("scripts/run-hosted-source-sync.sh")
        required = (
            "checkout_jobs=2",
            "telemetry_interval_seconds=60",
            "sync_started_epoch=",
            "source-sync.log",
            "source-sync-telemetry.log",
            "[source-sync heartbeat]",
            "elapsed_seconds=",
            "workspace_available_bytes=",
            "cat /proc/loadavg",
            "free -h",
            'df -h / "$workspace_root"',
            "ps -eo pid,ppid,state,pcpu,pmem,rss,vsz,etimes,comm --sort=-rss",
            "trap stop_observer EXIT",
            "trap handle_signal INT TERM",
            'pipeline_status=("${PIPESTATUS[@]}")',
            '--jobs "$checkout_jobs"',
            '2>&1 | tee "$sync_log"',
            'sync_status=${pipeline_status[0]:-125}',
            'tee_status=${pipeline_status[1]:-125}',
        )
        for value in required:
            with self.subTest(value=value):
                self.assertIn(value, script)
        self.assertNotIn("REPO_SYNC_NETWORK_JOBS", script)

    def test_build_and_rehearsal_use_the_same_observed_sync_wrapper(self) -> None:
        for name, workflow in (
            ("build", self._text(".github/workflows/build.yml")),
            ("rehearsal", self._patch_rehearsal_job()),
        ):
            with self.subTest(name=name):
                self.assertEqual(
                    workflow.count("bash scripts/run-hosted-source-sync.sh"),
                    1,
                )
                expected_debug = (
                    '--debug-dir "$DEBUG_DIR"'
                    if name == "build"
                    else "--debug-dir out/debug"
                )
                self.assertIn(expected_debug, workflow)
                self.assertNotIn("bash scripts/sync-sources.sh", workflow)
                self.assertIn("out/debug", workflow)
                if name == "rehearsal":
                    create_source = workflow.index("mkdir -p out/source")
                    observed_sync = workflow.index(
                        "bash scripts/run-hosted-source-sync.sh"
                    )
                    self.assertLess(create_source, observed_sync)

    def test_oos16_compile_supersedes_a_duplicate_patch_rehearsal(self) -> None:
        workflow = self._text(".github/workflows/validate.yml")
        rehearsal = self._patch_rehearsal_job()
        self.assertNotIn("- base: oos16", rehearsal)
        self.assertEqual(rehearsal.count("- base: oos15-global"), 1)
        self.assertEqual(rehearsal.count("- base: oos15-cn"), 1)

        start = workflow.index("  oos16-full-build:\n")
        full_build = workflow[start:]
        for value in (
            "base: oos16",
            "root: kernelsu-next",
            "profile: full",
            "target: mixed",
            "clean: true",
            "debug: true",
        ):
            with self.subTest(value=value):
                self.assertIn(value, full_build)


if __name__ == "__main__":
    unittest.main()
