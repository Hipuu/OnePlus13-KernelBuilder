from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "kernel_artifact_bundle",
    ROOT / "scripts" / "kernel-artifact-bundle.py",
)
assert SPEC is not None and SPEC.loader is not None
BUNDLE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = BUNDLE
SPEC.loader.exec_module(BUNDLE)


class KernelArtifactBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.bundle = self.root / "bundle"
        self.bundle.mkdir()
        self.restored = self.root / "restored"
        (self.restored / ".op13").mkdir(parents=True)
        self.identity = BUNDLE.BundleIdentity(
            repository="Hipuu/OnePlus13-KernelBuilder",
            head_sha="1" * 40,
            run_id=12345,
            run_attempt=2,
            artifact_name=(
                "kernel-build-oos16-kernelsu-next-full-mixed-O3-full-"
                "OnePlus13-KernelBuilder-timestamp-default"
            ),
            base="oos16",
            root="kernelsu-next",
            profile="full",
            optimization="O3",
            lto="full",
            branding="OnePlus13-KernelBuilder",
            build_timestamp="",
        )
        self.context_path = self.restored / ".op13" / "build-context.json"
        self._write_context()
        self._write_archive_contract()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _context_document(self) -> dict[str, object]:
        timestamp = BUNDLE.timestamp_request(self.identity.build_timestamp)
        document: dict[str, object] = {
            "kind": "oneplus13-build-context",
            "schema_version": 1,
            "stage": "packaged",
            "smoke": False,
            "device": "oneplus13",
            "profile": self.identity.base,
            "target": "sun",
            "arch": "arm64",
            "kmi": "android15-6.6",
            "configuration": {
                "profile": self.identity.base,
                "feature_profile": self.identity.profile,
                "root_variant": self.identity.root,
                "build_target": "mixed",
                "optimization": self.identity.optimization,
                "lto": self.identity.lto,
            },
            "kernel": {
                "build_target": "mixed",
                "branding": self.identity.branding,
                "source_date_epoch": 1700000000,
                "build_timestamp": {
                    "artifact_key": timestamp.artifact_key,
                    "mode": timestamp.mode,
                    "requested": timestamp.requested,
                    "requested_sha256": timestamp.requested_sha256,
                    "source_date_epoch": 1700000000,
                },
            },
            "packages": [{"path": "Image", "sha256": "2" * 64, "size": 1}],
        }
        document["context_sha256"] = BUNDLE._context_digest(document)
        return document

    def _write_context(self, document: dict[str, object] | None = None) -> None:
        value = self._context_document() if document is None else document
        self.context_path.write_text(
            json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    def _write_archive_contract(self, *, canonical: bool = True) -> None:
        archive = self.bundle / BUNDLE.ARCHIVE_NAME
        archive.write_bytes(b"fixture-zstd-frame")
        context = self.context_path.read_bytes()
        manifest = {
            "archive": {
                "compression": "zstd",
                "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "size": archive.stat().st_size,
                "tar_sha256": "3" * 64,
                "tar_size": 10240,
            },
            "exclusions": [
                "modules",
                ".op13/config-work",
                ".op13/config-work-msm-kernel",
            ],
            "format": BUNDLE.ARCHIVE_FORMAT,
            "members": [
                {"mode": 0o755, "path": ".op13", "sha256": "4" * 64,
                 "size": 0, "target": "", "type": "directory"},
                {"mode": 0o644, "path": BUNDLE.CONTEXT_PATH,
                 "sha256": hashlib.sha256(context).hexdigest(), "size": len(context),
                 "target": "", "type": "file"},
            ],
            "version": BUNDLE.ARCHIVE_VERSION,
        }
        payload = (
            BUNDLE._canonical_json_bytes(manifest)
            if canonical
            else (json.dumps(manifest, indent=2) + "\n").encode("ascii")
        )
        (self.bundle / BUNDLE.ARCHIVE_MANIFEST_NAME).write_bytes(payload)

    def _seal(self) -> dict[str, object]:
        return BUNDLE.seal_bundle(self.bundle, self.context_path, self.identity)

    def _identity_for_timestamp(self, raw: str):
        request = BUNDLE.timestamp_request(raw)
        prefix = self.identity.artifact_name.rsplit("-timestamp-", 1)[0]
        return replace(
            self.identity,
            artifact_name=f"{prefix}-timestamp-{request.artifact_key}",
            build_timestamp=raw,
        )

    def test_seal_and_verify_exact_bundle_and_restored_context(self) -> None:
        sealed = self._seal()
        self.assertEqual(sealed["status"], "verified")
        self.assertEqual(
            {path.name for path in self.bundle.iterdir()}, BUNDLE.BUNDLE_NAMES
        )
        checksums = (self.bundle / BUNDLE.CHECKSUM_NAME).read_bytes()
        self.assertNotIn(b"\r", checksums)
        self.assertTrue(checksums.endswith(b"\n"))
        self.assertEqual(
            [line.split(b"  ", 1)[1].decode() for line in checksums.splitlines()],
            list(BUNDLE.SEALED_NAMES),
        )
        verified = BUNDLE.verify_bundle(
            self.bundle, self.identity, restored_dir=self.restored
        )
        self.assertTrue(verified["restored_context_verified"])

    def test_exact_namespace_rejects_an_extra_member(self) -> None:
        self._seal()
        (self.bundle / "extra").write_text("unexpected", encoding="ascii")
        with self.assertRaisesRegex(BUNDLE.BundleError, "namespace differs"):
            BUNDLE.verify_bundle(self.bundle, self.identity, restored_dir=None)

    def test_exact_namespace_rejects_a_symlink_member(self) -> None:
        self._seal()
        checksum = self.bundle / BUNDLE.CHECKSUM_NAME
        checksum.unlink()
        try:
            checksum.symlink_to(BUNDLE.PROVENANCE_NAME)
        except (OSError, NotImplementedError):
            self.skipTest("symbolic links are unavailable")
        with self.assertRaisesRegex(BUNDLE.BundleError, "plain regular file"):
            BUNDLE.verify_bundle(self.bundle, self.identity, restored_dir=None)

    def test_checksum_file_must_have_canonical_order_and_lf(self) -> None:
        self._seal()
        checksum = self.bundle / BUNDLE.CHECKSUM_NAME
        lines = checksum.read_text(encoding="ascii").splitlines()
        checksum.write_bytes(("\r\n".join(reversed(lines)) + "\r\n").encode("ascii"))
        with self.assertRaisesRegex(BUNDLE.BundleError, "LF-terminated"):
            BUNDLE.verify_bundle(self.bundle, self.identity, restored_dir=None)

    def test_provenance_binds_every_expected_identity_field(self) -> None:
        self._seal()
        mutations = {
            "repository": "Other/Repo",
            "head_sha": "5" * 40,
            "run_id": 999,
            "run_attempt": 3,
            "artifact_name": "kernel-build-other-timestamp-default",
            "base": "oos15-global",
            "root": "kernelsu",
            "profile": "wild",
            "optimization": "O2",
            "lto": "thin",
            "branding": "OtherBrand",
        }
        for field, value in mutations.items():
            with self.subTest(field=field):
                with self.assertRaisesRegex(
                    BUNDLE.BundleError,
                    "provenance differs|exact build identity",
                ):
                    BUNDLE.verify_bundle(
                        self.bundle,
                        replace(self.identity, **{field: value}),
                        restored_dir=None,
                    )

    def test_artifact_name_must_encode_the_entire_exact_identity(self) -> None:
        mutations = (
            self.identity.artifact_name.replace("oos16", "oos15-cn", 1),
            self.identity.artifact_name.replace("-full-mixed-", "-wild-mixed-", 1),
            self.identity.artifact_name.replace("-mixed-", "-kernel-", 1),
            "prefix-" + self.identity.artifact_name,
        )
        for artifact_name in mutations:
            with self.subTest(artifact_name=artifact_name):
                with self.assertRaisesRegex(
                    BUNDLE.BundleError, "exact build identity"
                ):
                    BUNDLE._validate_identity(
                        replace(self.identity, artifact_name=artifact_name)
                    )

    def test_verify_can_accept_an_earlier_attempt_of_the_same_run(self) -> None:
        self._seal()
        later_run = replace(self.identity, run_attempt=self.identity.run_attempt + 1)
        with self.assertRaisesRegex(BUNDLE.BundleError, "provenance differs"):
            BUNDLE.verify_bundle(self.bundle, later_run, restored_dir=None)
        result = BUNDLE.verify_bundle(
            self.bundle,
            later_run,
            restored_dir=None,
            allow_earlier_run_attempt=True,
        )
        self.assertEqual(result["run_attempt"], self.identity.run_attempt)
        self.assertEqual(result["source_run_attempt"], later_run.run_attempt)

    def test_verify_never_accepts_a_future_attempt(self) -> None:
        self._seal()
        earlier_run = replace(self.identity, run_attempt=self.identity.run_attempt - 1)
        with self.assertRaisesRegex(BUNDLE.BundleError, "newer than the source run"):
            BUNDLE.verify_bundle(
                self.bundle,
                earlier_run,
                restored_dir=None,
                allow_earlier_run_attempt=True,
            )

    def test_seal_rejects_context_digest_not_present_in_archive_manifest(self) -> None:
        self.context_path.write_bytes(self.context_path.read_bytes() + b" ")
        with self.assertRaisesRegex(BUNDLE.BundleError, "differs from the archive manifest"):
            self._seal()

    def test_seal_rejects_context_semantics_even_when_hash_is_current(self) -> None:
        document = self._context_document()
        document["profile"] = "oos15-cn"
        document["context_sha256"] = BUNDLE._context_digest(document)
        self._write_context(document)
        self._write_archive_contract()
        with self.assertRaisesRegex(BUNDLE.BundleError, "platform or stage differs"):
            self._seal()

    def test_archive_manifest_must_be_canonical_json(self) -> None:
        self._write_archive_contract(canonical=False)
        with self.assertRaisesRegex(BUNDLE.BundleError, "canonical JSON"):
            self._seal()

    def test_explicit_timestamp_must_equal_context_source_date_epoch(self) -> None:
        self.identity = self._identity_for_timestamp("1700000001")
        self._write_context()
        self._write_archive_contract()
        with self.assertRaisesRegex(BUNDLE.BundleError, "explicit build timestamp"):
            self._seal()

    def test_context_must_bind_the_complete_raw_timestamp_record(self) -> None:
        document = self._context_document()
        timestamp = document["kernel"]["build_timestamp"]
        timestamp["requested"] = "1700000000"
        timestamp["requested_sha256"] = hashlib.sha256(b"1700000000").hexdigest()
        document["context_sha256"] = BUNDLE._context_digest(document)
        self._write_context(document)
        self._write_archive_contract()
        with self.assertRaisesRegex(BUNDLE.BundleError, "build_timestamp record"):
            self._seal()

    def test_equivalent_explicit_timestamps_have_distinct_artifact_identity(self) -> None:
        self.identity = self._identity_for_timestamp("1700000000")
        self._write_context()
        self._write_archive_contract()
        self._seal()
        equivalent = self._identity_for_timestamp("2023-11-14T22:13:20Z")
        self.assertEqual(
            BUNDLE.timestamp_request(self.identity.build_timestamp).explicit_epoch,
            BUNDLE.timestamp_request(equivalent.build_timestamp).explicit_epoch,
        )
        self.assertNotEqual(self.identity.artifact_name, equivalent.artifact_name)
        with self.assertRaisesRegex(BUNDLE.BundleError, "provenance differs"):
            BUNDLE.verify_bundle(self.bundle, equivalent, restored_dir=None)

    def test_exact_raw_timestamp_is_bound_even_when_trimmed_value_is_equal(self) -> None:
        self.identity = self._identity_for_timestamp(" 1700000000 ")
        self._write_context()
        self._write_archive_contract()
        self._seal()
        trimmed = self._identity_for_timestamp("1700000000")
        self.assertEqual(
            BUNDLE.timestamp_request(self.identity.build_timestamp).explicit_epoch,
            BUNDLE.timestamp_request(trimmed.build_timestamp).explicit_epoch,
        )
        self.assertNotEqual(self.identity.artifact_name, trimmed.artifact_name)
        with self.assertRaisesRegex(BUNDLE.BundleError, "provenance differs"):
            BUNDLE.verify_bundle(self.bundle, trimmed, restored_dir=None)

    def test_default_timestamp_records_and_post_restore_checks_context_epoch(self) -> None:
        self._seal()
        provenance_path = self.bundle / BUNDLE.PROVENANCE_NAME
        provenance = json.loads(provenance_path.read_text(encoding="ascii"))
        timestamp = provenance["build"]["build_timestamp"]
        self.assertEqual(
            timestamp,
            {
                "artifact_key": "default",
                "mode": "default",
                "requested": None,
                "requested_sha256": None,
                "source_date_epoch": 1700000000,
            },
        )
        timestamp["source_date_epoch"] = 1700000001
        provenance_path.write_bytes(BUNDLE._canonical_json_bytes(provenance))
        files = {
            name: self.bundle / name
            for name in BUNDLE.BUNDLE_NAMES - {BUNDLE.CHECKSUM_NAME}
        }
        (self.bundle / BUNDLE.CHECKSUM_NAME).write_bytes(
            BUNDLE._checksum_payload(files)
        )
        pre_restore = BUNDLE.verify_bundle(
            self.bundle, self.identity, restored_dir=None
        )
        self.assertFalse(pre_restore["restored_context_verified"])
        with self.assertRaisesRegex(BUNDLE.BundleError, "artifact provenance"):
            BUNDLE.verify_bundle(
                self.bundle, self.identity, restored_dir=self.restored
            )

    def test_timestamp_parser_matches_build_epoch_bounds_and_timezone_rules(self) -> None:
        self.assertEqual(BUNDLE.timestamp_request("").artifact_key, "default")
        self.assertEqual(
            BUNDLE.timestamp_request("1970-01-01T00:00:00Z").explicit_epoch, 0
        )
        for raw in (" ", "1969-12-31T23:59:59Z", "2108-01-01T00:00:00Z",
                    "2023-11-14T22:13:20", "not-a-timestamp"):
            with self.subTest(raw=raw):
                with self.assertRaises(BUNDLE.BundleError):
                    BUNDLE.timestamp_request(raw)

    def _run(self) -> dict[str, object]:
        return {
            "id": self.identity.run_id,
            "run_attempt": self.identity.run_attempt,
            "head_sha": self.identity.head_sha,
            "repository": {
                "id": 1301214051,
                "full_name": self.identity.repository,
            },
            "head_repository": {
                "id": 1301214051,
                "full_name": self.identity.repository,
            },
            "path": ".github/workflows/build.yml",
            "event": "workflow_dispatch",
            "status": "completed",
            "conclusion": "success",
        }

    def _verify_run(
        self,
        document: dict[str, object],
        *,
        requested: int | None = None,
        current: int = 77777,
    ) -> dict[str, object]:
        return BUNDLE.verify_workflow_run(
            document,
            repository=self.identity.repository,
            repository_id=1301214051,
            head_sha=self.identity.head_sha,
            run_id=self.identity.run_id,
            requested_run_id=requested,
            current_run_id=current,
        )

    def test_trusted_completed_build_run_is_accepted(self) -> None:
        result = self._verify_run(self._run())
        self.assertEqual(result["status"], "trusted")
        self.assertEqual(result["run_attempt"], self.identity.run_attempt)

    def test_current_release_run_requires_explicit_matching_request(self) -> None:
        run = self._run()
        run.update(
            {
                "path": ".github/workflows/release.yml",
                "status": "in_progress",
                "conclusion": None,
            }
        )
        result = self._verify_run(
            run,
            requested=self.identity.run_id,
            current=self.identity.run_id,
        )
        self.assertEqual(result["status"], "trusted")
        for requested, current in ((None, self.identity.run_id), (999, 999),
                                   (self.identity.run_id, 999)):
            with self.subTest(requested=requested, current=current):
                with self.assertRaises(BUNDLE.BundleError):
                    self._verify_run(run, requested=requested, current=current)

    def test_workflow_run_rejects_untrusted_origin_or_state(self) -> None:
        mutations = [
            ("path", ".github/workflows/validate.yml"),
            ("event", "push"),
            ("status", "in_progress"),
            ("conclusion", "failure"),
            ("head_sha", "6" * 40),
        ]
        for field, value in mutations:
            with self.subTest(field=field):
                run = self._run()
                run[field] = value
                with self.assertRaises(BUNDLE.BundleError):
                    self._verify_run(run)
        for repository_field in ("repository", "head_repository"):
            with self.subTest(repository_field=repository_field):
                run = self._run()
                assert isinstance(run[repository_field], dict)
                run[repository_field] = copy.deepcopy(run[repository_field])
                run[repository_field]["id"] = 42
                with self.assertRaisesRegex(BUNDLE.BundleError, "fork"):
                    self._verify_run(run)


if __name__ == "__main__":
    unittest.main()
