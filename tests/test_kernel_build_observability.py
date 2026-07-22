from __future__ import annotations

import os
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lib.build import (
    OOS16_HOSTED_EXTRA_KBUILD_ARGS,
    _effective_kernel_resource_policy,
)


class KernelBuildObservabilityTests(unittest.TestCase):
    @staticmethod
    def _text(relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def _run_fixture(
        self,
        root: Path,
        *,
        status: int,
    ) -> subprocess.CompletedProcess[str]:
        if os.name == "nt":
            self.skipTest("hosted-runner observer execution requires Linux /proc")
        bash = None
        git = shutil.which("git")
        if os.name == "nt" and git is not None:
            candidate = Path(git).resolve().parent.parent / "bin" / "bash.exe"
            if candidate.is_file():
                bash = str(candidate)
        if bash is None:
            bash = shutil.which("bash")
        if bash is None:
            self.skipTest("bash is unavailable")
        scripts = root / "scripts"
        debug = root / "out" / "debug"
        scripts.mkdir(parents=True)
        debug.mkdir(parents=True)
        (scripts / "run-observed-kernel-build.sh").write_bytes(
            (ROOT / "scripts" / "run-observed-kernel-build.sh").read_bytes()
        )
        (scripts / "build-kernel.sh").write_text(
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' \"$@\" > \"$GITHUB_WORKSPACE/build-args.txt\"\n"
            "exit \"${FIXTURE_STATUS:-0}\"\n",
            encoding="utf-8",
            newline="\n",
        )
        environment = dict(os.environ)
        environment.update(
            {
                "GITHUB_WORKSPACE": str(root.resolve()),
                "FIXTURE_STATUS": str(status),
            }
        )
        return subprocess.run(
            [
                bash,
                "scripts/run-observed-kernel-build.sh",
                "--debug-dir",
                "out/debug",
                "--",
                "bash",
                "scripts/build-kernel.sh",
                "--source-dir",
                "out/source",
                "--output",
                "out/build",
                "--debug",
            ],
            cwd=root,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=15,
            check=False,
        )

    def test_observer_contract_is_one_minute_and_environment_only(self) -> None:
        script = self._text("scripts/run-observed-kernel-build.sh")
        required = (
            "kernel-build-telemetry.log",
            "telemetry_interval_seconds=60",
            "date -u +'%Y-%m-%dT%H:%M:%SZ'",
            "elapsed_seconds=",
            "load1=",
            "MemAvailable",
            "swap_total_kib=",
            "swap_free_kib=",
            "swap_used_kib=",
            "workspace_available_bytes=",
            "highest RSS processes",
            "ps -eo pid,ppid,state,pcpu,pmem,rss,vsz,etimes,comm --sort=-rss",
            "[kernel-build heartbeat]",
            "trap cleanup EXIT",
            "trap 'handle_signal INT 130' INT",
            "trap 'handle_signal TERM 143' TERM",
            "trap stop_observer_sleep INT TERM",
            'kill "$sleep_pid"',
            'setsid -- "${command[@]}" &',
            'build_pid=$!',
            'build_pgid=$build_pid',
            'kill -s "$signal_name" -- "-$build_pgid"',
            "queue_signal() {",
            "pending_signal=$1",
            'handle_signal "$pending_signal" "$pending_status"',
            'wait "$build_pid"',
            "build_status=$?",
            "stop_build_group TERM",
            'exit "$build_status"',
        )
        for value in required:
            with self.subTest(value=value):
                self.assertIn(value, script)
        for limit in (
            "--jobs",
            "--local_ram_resources",
            "--local_cpu_resources",
            "BAZEL_JOBS",
            "ulimit",
        ):
            with self.subTest(limit=limit):
                self.assertNotIn(limit, script)

    def test_wrapper_records_initial_and_final_snapshots_and_preserves_args(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = self._run_fixture(root, status=0)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("[kernel-build heartbeat]", result.stdout)
            log = (
                root / "out" / "debug" / "kernel-build-telemetry.log"
            ).read_text(encoding="utf-8")
            self.assertGreaterEqual(log.count("=== "), 2)
            for value in (
                "elapsed_seconds=",
                "load1=",
                "mem_available_kib=",
                "swap_used_kib=",
                "workspace_available_bytes=",
                "--- highest RSS processes (KiB) ---",
            ):
                with self.subTest(value=value):
                    self.assertIn(value, log)
            self.assertEqual(
                (root / "build-args.txt").read_text(encoding="utf-8").splitlines(),
                [
                    "--source-dir",
                    "out/source",
                    "--output",
                    "out/build",
                    "--debug",
                ],
            )

    def test_wrapper_propagates_kernel_build_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = self._run_fixture(root, status=23)
            self.assertEqual(result.returncode, 23, result.stdout)
            self.assertTrue(
                (root / "out" / "debug" / "kernel-build-telemetry.log").is_file()
            )

    def test_exited_launcher_does_not_leave_background_descendants(self) -> None:
        if os.name == "nt" or not sys.platform.startswith("linux"):
            self.skipTest("process-group descendant cleanup requires Linux")
        bash = shutil.which("bash")
        if bash is None or shutil.which("setsid") is None:
            self.skipTest("bash and setsid are required")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scripts = root / "scripts"
            debug = root / "out" / "debug"
            scripts.mkdir(parents=True)
            debug.mkdir(parents=True)
            (scripts / "run-observed-kernel-build.sh").write_bytes(
                (ROOT / "scripts" / "run-observed-kernel-build.sh").read_bytes()
            )
            (scripts / "build-kernel.sh").write_text(
                "#!/usr/bin/env bash\n"
                "sleep 300 &\n"
                "printf '%s\\n' \"$!\" > \"$GITHUB_WORKSPACE/orphan.pid\"\n"
                "exit 23\n",
                encoding="utf-8",
                newline="\n",
            )
            environment = dict(os.environ)
            environment["GITHUB_WORKSPACE"] = str(root.resolve())
            result = subprocess.run(
                [
                    bash,
                    "scripts/run-observed-kernel-build.sh",
                    "--debug-dir",
                    "out/debug",
                    "--",
                    "bash",
                    "scripts/build-kernel.sh",
                ],
                cwd=root,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=15,
                check=False,
            )
            self.assertEqual(result.returncode, 23, result.stdout)
            child_pid = int((root / "orphan.pid").read_text(encoding="utf-8"))
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and Path(f"/proc/{child_pid}").exists():
                time.sleep(0.05)
            self.assertFalse(
                Path(f"/proc/{child_pid}").exists(),
                f"observer left descendant {child_pid} alive",
            )

    def _run_signal_forwarding_case(self, signal_number: int, status: int) -> None:
        if os.name == "nt" or not sys.platform.startswith("linux"):
            self.skipTest("process-group cancellation requires Linux")
        bash = shutil.which("bash")
        if bash is None or shutil.which("setsid") is None:
            self.skipTest("bash and setsid are required")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scripts = root / "scripts"
            debug = root / "out" / "debug"
            scripts.mkdir(parents=True)
            debug.mkdir(parents=True)
            (scripts / "run-observed-kernel-build.sh").write_bytes(
                (ROOT / "scripts" / "run-observed-kernel-build.sh").read_bytes()
            )
            (scripts / "build-kernel.sh").write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "child_pid=\n"
                "finish() {\n"
                "  if [[ -n \"$child_pid\" ]]; then\n"
                "    wait \"$child_pid\" 2>/dev/null || true\n"
                "  fi\n"
                "  exit 0\n"
                "}\n"
                "trap finish INT TERM\n"
                "sleep 300 &\n"
                "child_pid=$!\n"
                "printf '%s\\n' \"$$\" > \"$GITHUB_WORKSPACE/build-shell.pid\"\n"
                "printf '%s\\n' \"$child_pid\" > \"$GITHUB_WORKSPACE/build-child.pid\"\n"
                "wait \"$child_pid\"\n",
                encoding="utf-8",
                newline="\n",
            )
            environment = dict(os.environ)
            environment["GITHUB_WORKSPACE"] = str(root.resolve())
            process = subprocess.Popen(
                [
                    bash,
                    "scripts/run-observed-kernel-build.sh",
                    "--debug-dir",
                    "out/debug",
                    "--",
                    "bash",
                    "scripts/build-kernel.sh",
                ],
                cwd=root,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            try:
                pid_paths = [root / "build-shell.pid", root / "build-child.pid"]
                deadline = time.monotonic() + 10
                while not all(path.is_file() for path in pid_paths):
                    if process.poll() is not None:
                        output = process.stdout.read() if process.stdout else ""
                        self.fail(f"observer exited before fixture start: {output}")
                    if time.monotonic() >= deadline:
                        self.fail("timed out waiting for cancellable build fixture")
                    time.sleep(0.05)
                build_pids = [int(path.read_text(encoding="utf-8")) for path in pid_paths]
                process.send_signal(signal_number)
                output, _ = process.communicate(timeout=15)
                self.assertEqual(process.returncode, status, output)

                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if all(not Path(f"/proc/{pid}").exists() for pid in build_pids):
                        break
                    time.sleep(0.05)
                self.assertTrue(
                    all(not Path(f"/proc/{pid}").exists() for pid in build_pids),
                    f"cancelled build left a live process: {build_pids}",
                )
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)

    def test_term_is_forwarded_without_orphaning_build_processes(self) -> None:
        self._run_signal_forwarding_case(signal.SIGTERM, 143)

    def test_int_is_forwarded_without_orphaning_build_processes(self) -> None:
        self._run_signal_forwarding_case(signal.SIGINT, 130)

    def test_workflow_wraps_only_kernel_build_without_cli_resource_flags(self) -> None:
        workflow = self._text(".github/workflows/build.yml")
        start = workflow.index("      - name: Build kernel\n")
        end = workflow.index(
            "\n      - name: Measure audited Bazel cache for save",
            start,
        )
        step = workflow[start:end]
        self.assertEqual(
            step.count("bash scripts/run-observed-kernel-build.sh"),
            1,
        )
        self.assertEqual(step.count("-- bash scripts/build-kernel.sh"), 1)
        self.assertIn('--debug-dir "$DEBUG_DIR"', step)
        self.assertIn('"${args[@]}"', step)
        for limit in (
            "--local_cpu_resources",
            "BAZEL_JOBS",
            "ulimit",
        ):
            with self.subTest(limit=limit):
                self.assertNotIn(limit, step)

    def test_workflow_applies_one_constant_oos16_resource_policy(self) -> None:
        workflow = self._text(".github/workflows/build.yml")
        start = workflow.index("      - name: Build kernel\n")
        end = workflow.index(
            "\n      - name: Measure audited Bazel cache for save",
            start,
        )
        step = workflow[start:end]
        expected = (
            "          EXTRA_KBUILD_ARGS: ${{ inputs.base == 'oos16' && "
            "'--jobs=2 --local_ram_resources=8192' || '' }}\n"
        )
        self.assertIn(expected, step)
        self.assertEqual(workflow.count("EXTRA_KBUILD_ARGS:"), 1)
        inputs = workflow[: workflow.index("jobs:\n")]
        self.assertNotIn("EXTRA_KBUILD_ARGS", inputs)

    def test_build_policy_overrides_inherited_values_deterministically(self) -> None:
        with patch.dict(
            os.environ,
            {
                "GITHUB_ACTIONS": "true",
                "EXTRA_KBUILD_ARGS": "$(untrusted inherited value)",
            },
            clear=False,
        ):
            oos16 = _effective_kernel_resource_policy("oos16")
            oos15 = _effective_kernel_resource_policy("oos15-cn")
        self.assertEqual(
            oos16["extra_kbuild_args"],
            OOS16_HOSTED_EXTRA_KBUILD_ARGS,
        )
        self.assertEqual(oos16["bazel_jobs"], 2)
        self.assertEqual(oos16["local_ram_resources_mib"], 8192)
        self.assertEqual(oos15["policy"], "tool-default")
        self.assertIsNone(oos15["extra_kbuild_args"])

        with patch.dict(
            os.environ,
            {
                "GITHUB_ACTIONS": "",
                "EXTRA_KBUILD_ARGS": "--jobs=99",
            },
            clear=False,
        ):
            local_oos16 = _effective_kernel_resource_policy("oos16")
        self.assertEqual(local_oos16["policy"], "tool-default")
        self.assertIsNone(local_oos16["extra_kbuild_args"])


if __name__ == "__main__":
    unittest.main()
