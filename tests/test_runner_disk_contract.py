from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class HostedRunnerDiskContractTests(unittest.TestCase):
    ACTION = (
        "easimon/maximize-build-space@"
        "c28619d8999a147d5e09c1199f84ff6af6ad5794"
    )

    @staticmethod
    def _text(relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def _patch_rehearsal_job(self) -> str:
        text = self._text(".github/workflows/validate.yml")
        start = text.index("  patch-rehearsal:\n")
        end = text.index("\n  oos16-full-build:\n", start)
        return text[start:end]

    def test_build_and_rehearsal_pool_disks_before_checkout(self) -> None:
        build_job = self._text(".github/workflows/build.yml")
        rehearsal_job = self._patch_rehearsal_job()
        for name, text in (("build", build_job), ("rehearsal", rehearsal_job)):
            with self.subTest(job=name):
                self.assertEqual(text.count(self.ACTION), 1)
                cleanup = text.index(
                    "- name: Reclaim allowlisted runner tools before pooling disks"
                )
                pooling = text.index(self.ACTION)
                capture = text.index(
                    "- name: Capture failed pre-LVM setup state", pooling
                )
                evidence = text.index(
                    "- name: Preserve failed pre-LVM setup evidence", pooling
                )
                checkout = text.index("- name: Check out repository", pooling)
                validation = text.index(
                    "bash scripts/validate-runner-storage.sh", checkout
                )
                self.assertLess(cleanup, pooling)
                self.assertLess(pooling, capture)
                self.assertLess(capture, evidence)
                self.assertLess(evidence, checkout)
                self.assertLess(checkout, validation)
                if name == "build":
                    self.assertLess(
                        validation, text.index("- name: Validate inputs and runner tools")
                    )
                else:
                    self.assertLess(
                        validation, text.index("- name: Synchronize locked sources")
                    )

                self.assertIn("root-reserve-mb: 8448", text)
                self.assertIn("temp-reserve-mb: 1024", text)
                self.assertIn("swap-size-mb: 4096", text)
                self.assertIn("overprovision-lvm: 'false'", text)
                self.assertIn("build-mount-path: ${{ github.workspace }}", text)
                self.assertIn("build-mount-path-ownership: runner:runner", text)
                self.assertIn("pv-loop-path: /pv.img", text)
                self.assertIn("tmp-pv-loop-path: /mnt/tmp-pv.img", text)
                self.assertNotIn("build-mount-path: ${{ github.workspace }}/out", text)
                self.assertIn("remove-dotnet: 'false'", text)
                self.assertIn("remove-android: 'false'", text)
                self.assertIn("remove-haskell: 'false'", text)
                self.assertIn("remove-codeql: 'false'", text)
                self.assertIn("remove-docker-images: 'false'", text)
                self.assertIn("- name: Capture failed pre-LVM setup state", text)
                self.assertIn("df -h || true", text)
                self.assertIn("sudo losetup --list || true", text)
                self.assertIn("sudo pvs || true", text)
                self.assertIn("sudo lvs || true", text)
                self.assertIn("path: ${{ runner.temp }}/op13-lvm-failure", text)
                self.assertIn("if-no-files-found: warn", text)

    def test_pooled_disk_action_is_immutable(self) -> None:
        match = re.fullmatch(r"[^@]+@([0-9a-f]{40})", self.ACTION)
        self.assertIsNotNone(match)

    def test_build_and_rehearsal_share_the_same_pooling_preamble(self) -> None:
        marker = "      - name: Reclaim allowlisted runner tools before pooling disks\n"
        end_marker = "      - name: Capture failed pre-LVM setup state\n"

        def preamble(text: str) -> str:
            start = text.index(marker)
            end = text.index(end_marker, start)
            return text[start:end]

        self.assertEqual(
            preamble(self._text(".github/workflows/build.yml")),
            preamble(self._patch_rehearsal_job()),
        )

    def test_source_and_build_jobs_reclaim_only_explicit_paths(self) -> None:
        for relative, text in (
            (".github/workflows/build.yml", self._text(".github/workflows/build.yml")),
            (".github/workflows/validate.yml", self._patch_rehearsal_job()),
        ):
            with self.subTest(workflow=relative):
                self.assertIn("/opt/hostedtoolcache\n", text)
                self.assertNotIn("/opt/hostedtoolcache/CodeQL", text)
                self.assertIn("resolved=$(realpath -e -- \"$candidate\")", text)
                self.assertIn("sudo rm -rf --one-file-system -- \"$resolved\"", text)
                self.assertIn(
                    'if [[ "$resolved" != "$candidate" ]]; then', text
                )
                self.assertIn(
                    '[[ "$(stat -c %d -- "$resolved")" != "$root_device" ]]',
                    text,
                )
                self.assertIn('mountpoint -q -- "$resolved"', text)

    def test_shared_validator_enforces_lvm_layout_and_capacity(self) -> None:
        script = self._text("scripts/validate-runner-storage.sh")
        required_contracts = (
            'if ! mountpoint -q -- "$workspace_root"',
            '[[ "$workspace_device" == "$root_device"',
            'mount_type" != ext4',
            '*,rw,*)',
            "/dev/mapper/buildvg-buildlv",
            "sudo losetup --associated /pv.img",
            "sudo losetup --associated /mnt/tmp-pv.img",
            'if ((${#build_pvs[@]} != 2))',
            "/dev/mapper/buildvg-swap",
            "sudo swapon --show=NAME --noheadings",
            "minimum_root_available=$((8 * 1024 * 1024 * 1024))",
            "minimum_available=$((100 * 1024 * 1024 * 1024))",
            "less than 8 GiB remains reserved on the root filesystem",
            "less than 100 GiB is available on the pooled build filesystem",
            'if [[ -L "$out_root" ]]',
            'if [[ -L "$debug_dir" ]]',
        )
        for contract in required_contracts:
            with self.subTest(contract=contract):
                self.assertIn(contract, script)

    def test_workspace_paths_and_release_guard_remain_canonical(self) -> None:
        build = self._text(".github/workflows/build.yml")
        validate = self._text(".github/workflows/validate.yml")
        all_text = build + validate + self._text("scripts/validate-runner-storage.sh")
        self.assertIn("SOURCE_DIR: out/source", build)
        self.assertIn(
            'if [[ "$source_root" != "$workspace_root/out/source" ]]; then', build
        )
        self.assertIn('rm -rf --one-file-system -- "$source_root"', build)
        self.assertIn('df -h / "$GITHUB_WORKSPACE"', build)
        self.assertIn('df -h / "$GITHUB_WORKSPACE"', validate)
        self.assertNotIn("ln -s", all_text)


if __name__ == "__main__":
    unittest.main()
