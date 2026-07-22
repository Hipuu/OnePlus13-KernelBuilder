from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "build.yml"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


class WorkflowKernelArtifactContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")

    def _step(self, name: str, next_name: str) -> str:
        start = self.workflow.index(f"      - name: {name}\n")
        end = self.workflow.index(f"\n      - name: {next_name}\n", start)
        return self.workflow[start:end]

    def test_resolver_checks_artifact_and_source_run_trust(self) -> None:
        step = self._step(
            "Resolve modules-only kernel artifact", "Download matching kernel artifact"
        )
        required = (
            ".workflow_run.repository_id",
            ".workflow_run.head_repository_id",
            ".workflow_run.head_sha",
            "scripts/kernel-artifact-bundle.py verify-run",
            "--repository-id \"$GITHUB_REPOSITORY_ID\"",
            "--requested-run-id \"$REQUESTED_RUN_ID\"",
            '"/repos/${GITHUB_REPOSITORY}/actions/runs/${candidate_run_id}"',
            'echo "artifact_id=$selected_artifact_id"',
            "candidate_runs_seen",
            "more than one unexpired exact-name artifact",
        )
        for token in required:
            with self.subTest(token=token):
                self.assertIn(token, step)
        self.assertNotIn(".workflow_run.id // empty", step)
        self.assertIn("--paginate --slurp", step)
        self.assertIn("'[.[].artifacts[]]'", step)

        download = self._step(
            "Download matching kernel artifact", "Restore reusable kernel output"
        )
        self.assertIn(
            "artifact-ids: ${{ steps.kernel-artifact.outputs.artifact_id }}",
            download,
        )
        self.assertIn("digest-mismatch: error", download)
        self.assertNotIn("name: ${{ steps.artifact-names.outputs.kernel }}", download)

    def test_restore_verifies_bundle_then_uses_atomic_archive_helper(self) -> None:
        step = self._step("Restore reusable kernel output", "Synchronize locked sources")
        self.assertGreaterEqual(
            step.count("scripts/kernel-artifact-bundle.py verify"), 2
        )
        self.assertIn("scripts/kernel-artifact-archive.py restore", step)
        self.assertIn('--manifest "$KERNEL_ARTIFACT_DOWNLOAD_DIR/KERNEL-ARTIFACT-MANIFEST.json"', step)
        self.assertIn('rmdir -- "$module_root"', step)
        self.assertIn('rmdir -- "$build_root"', step)
        self.assertIn('--restored-dir "$BUILD_DIR"', step)
        self.assertIn('--build-timestamp "$BUILD_TIMESTAMP"', step)
        self.assertIn("--allow-earlier-run-attempt", step)
        for output in (".config", "Module.symvers", "System.map", ".op13/build-context.json"):
            self.assertIn(f'test -s "$BUILD_DIR/{output}"', step)
        self.assertNotIn("tar --", step)
        self.assertNotIn("sha256sum", step)
        self.assertNotIn("rm -rf", step)

    def test_creation_and_upload_are_mixed_prerequisite_only(self) -> None:
        create = self._step("Archive reusable kernel output", "Upload reusable kernel output")
        upload = self._step("Upload reusable kernel output", "Upload packages")
        self.assertIn("if: success() && inputs.target == 'mixed'", create)
        self.assertIn("if: success() && inputs.target == 'mixed'", upload)
        self.assertIn("scripts/kernel-artifact-archive.py create", create)
        self.assertIn("scripts/kernel-artifact-bundle.py seal", create)
        self.assertIn('--manifest "$KERNEL_ARTIFACT_DIR/KERNEL-ARTIFACT-MANIFEST.json"', create)
        self.assertIn('--build-context "$BUILD_DIR/.op13/build-context.json"', create)
        self.assertIn('--build-timestamp "$BUILD_TIMESTAMP"', create)
        self.assertNotIn("tar --", create)
        self.assertNotIn("sha256sum", create)

    def test_exact_bundle_names_are_declared_in_workflow(self) -> None:
        for name in (
            "kernel-build.tar.zst",
            "KERNEL-ARTIFACT-MANIFEST.json",
            "KERNEL-ARTIFACT-PROVENANCE.json",
            "SHA256SUMS",
        ):
            with self.subTest(name=name):
                self.assertIn(name, self.workflow)

    def test_artifact_names_bind_exact_timestamp_key(self) -> None:
        step = self._step("Resolve artifact names", "Resolve audited compile cache identity")
        self.assertIn("scripts/kernel-artifact-bundle.py timestamp-key", step)
        self.assertIn('--build-timestamp "$BUILD_TIMESTAMP"', step)
        self.assertIn('"$timestamp_key" != default', step)
        self.assertEqual(step.count("-timestamp-${timestamp_key}"), 2)
        self.assertIn('echo "timestamp_key=$timestamp_key"', step)

    def test_release_downloads_exact_uploaded_package_artifact(self) -> None:
        self.assertIn(
            "package_artifact_id: ${{ steps.upload-packages.outputs.artifact-id }}",
            self.workflow,
        )
        self.assertIn(
            "package_artifact_digest: ${{ steps.upload-packages.outputs.artifact-digest }}",
            self.workflow,
        )
        self.assertIn("id: upload-packages", self.workflow)
        self.assertEqual(self.workflow.count("          overwrite: true\n"), 2)
        self.assertIn("-attempt-${{ github.run_attempt }}", self.workflow)

        release = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        start = release.index(
            "      - name: Validate rebuilt package artifact identity\n"
        )
        end = release.index("\n      - name: Verify build checksums\n", start)
        steps = release[start:end]
        self.assertIn(
            "PACKAGE_ARTIFACT_ID: ${{ needs.rebuild.outputs.package_artifact_id }}",
            steps,
        )
        self.assertIn(
            "PACKAGE_ARTIFACT_DIGEST: ${{ needs.rebuild.outputs.package_artifact_digest }}",
            steps,
        )
        for token in (
            '"/repos/${GITHUB_REPOSITORY}/actions/artifacts/${PACKAGE_ARTIFACT_ID}"',
            ".digest == $digest",
            "(.workflow_run.id | tostring) == $run_id",
            ".workflow_run.head_sha == $head_sha",
            "(.workflow_run.repository_id | tostring) == $repository_id",
            "(.workflow_run.head_repository_id | tostring) == $repository_id",
        ):
            with self.subTest(token=token):
                self.assertIn(token, steps)
        self.assertIn(
            "artifact-ids: ${{ steps.rebuilt-package.outputs.artifact_id }}",
            steps,
        )
        self.assertIn("run-id: ${{ github.run_id }}", steps)
        self.assertIn("repository: ${{ github.repository }}", steps)
        self.assertIn("github-token: ${{ github.token }}", steps)
        self.assertIn("digest-mismatch: error", steps)
        self.assertNotIn("name: ${{ needs.rebuild.outputs.package_artifact }}", steps)


if __name__ == "__main__":
    unittest.main()
