from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "kernel-artifact-archive.py"
SPEC = importlib.util.spec_from_file_location("kernel_artifact_archive", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
ARCHIVE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ARCHIVE
SPEC.loader.exec_module(ARCHIVE)


def canonical_json(document: dict[str, object]) -> bytes:
    return (
        json.dumps(
            document,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("ascii")


def sealed_member(
    path: str,
    data: bytes = b"",
    *,
    kind: str = "file",
    mode: int = 0o644,
    digest_data: bytes | None = None,
    target: str = "",
) -> dict[str, object]:
    if kind == "directory":
        data = b""
        mode = 0o755 if mode == 0o644 else mode
    elif kind == "symlink":
        data = b""
        mode = 0o777
    return {
        "mode": mode,
        "path": path,
        "sha256": hashlib.sha256(data if digest_data is None else digest_data).hexdigest(),
        "size": len(data),
        "target": target,
        "type": kind,
    }


def tar_payload(entries: list[dict[str, object]]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for entry in entries:
            name = str(entry["name"])
            data = entry.get("data", b"")
            assert isinstance(data, bytes)
            info = tarfile.TarInfo(name)
            info.type = entry.get("type", tarfile.REGTYPE)
            default_mode = 0o777 if info.type == tarfile.SYMTYPE else 0o644
            if info.type == tarfile.DIRTYPE:
                default_mode = 0o755
            info.mode = int(entry.get("mode", default_mode))
            info.uid = int(entry.get("uid", 0))
            info.gid = int(entry.get("gid", 0))
            info.uname = str(entry.get("uname", ""))
            info.gname = str(entry.get("gname", ""))
            info.mtime = int(entry.get("mtime", 0))
            info.linkname = str(entry.get("linkname", ""))
            info.devmajor = int(entry.get("devmajor", 0))
            info.devminor = int(entry.get("devminor", 0))
            if info.type == tarfile.REGTYPE:
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
            else:
                info.size = int(entry.get("size", 0))
                archive.addfile(info)
    return output.getvalue()


def rewrite_tar_checksum(payload: bytearray, offset: int = 0) -> None:
    payload[offset + 148 : offset + 156] = b"        "
    checksum = sum(payload[offset : offset + tarfile.BLOCKSIZE])
    payload[offset + 148 : offset + 156] = f"{checksum:06o}\0 ".encode("ascii")


def raw_zstd_frame(payload: bytes) -> bytes:
    if len(payload) < 256 or len(payload) >= 256 + (1 << 16):
        raise AssertionError("test zstd frame helper requires a two-byte content size")
    descriptor = b"\x60"  # two-byte content size, single-segment, no checksum
    content_size = (len(payload) - 256).to_bytes(2, "little")
    block_header = ((len(payload) << 3) | 1).to_bytes(3, "little")
    return ARCHIVE.ZSTD_MAGIC + descriptor + content_size + block_header + payload


class KernelArtifactArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_fixture(
        self,
        payload: bytes,
        members: list[dict[str, object]],
        *,
        name: str = "kernel-build.tar",
    ) -> tuple[Path, Path]:
        archive_path = self.root / name
        archive_path.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        document: dict[str, object] = {
            "archive": {
                "compression": "none",
                "sha256": digest,
                "size": len(payload),
                "tar_sha256": digest,
                "tar_size": len(payload),
            },
            "exclusions": list(ARCHIVE.EXCLUDED_PREFIXES),
            "format": ARCHIVE.FORMAT_NAME,
            "members": members,
            "version": ARCHIVE.FORMAT_VERSION,
        }
        manifest_path = self.root / f"{name}.manifest.json"
        manifest_path.write_bytes(canonical_json(document))
        return archive_path, manifest_path

    def rewrite_manifest(self, path: Path, mutate: object) -> dict[str, object]:
        document = json.loads(path.read_text(encoding="ascii"))
        assert callable(mutate)
        mutate(document)
        path.write_bytes(canonical_json(document))
        return document

    def assert_rejected(
        self,
        payload: bytes,
        members: list[dict[str, object]],
    ) -> str:
        archive_path, manifest_path = self.write_fixture(payload, members)
        destination = self.root / "restored"
        with self.assertRaises(ARCHIVE.ArchiveError) as caught:
            ARCHIVE.restore_archive(archive_path, manifest_path, destination)
        self.assertFalse(destination.exists())
        self.assertFalse(
            any(self.root.glob(".restored.restore-*")),
            "failed extraction must remove its unpublished directory",
        )
        return str(caught.exception)

    def test_uncompressed_roundtrip_is_deterministic_and_exactly_excluded(self) -> None:
        source = self.root / "source"
        (source / ".op13" / "config-work").mkdir(parents=True)
        (source / ".op13" / "config-work-msm-kernel").mkdir()
        (source / "modules").mkdir()
        (source / "nested" / "modules").mkdir(parents=True)
        (source / "empty").mkdir()
        (source / ".config").write_bytes(b"CONFIG_TEST=y\n")
        (source / ".op13" / "build-context.json").write_bytes(b"{}\n")
        (source / ".op13" / "config-work" / "private").write_bytes(b"excluded\n")
        (source / ".op13" / "config-work-msm-kernel" / "private").write_bytes(
            b"excluded\n"
        )
        (source / "modules" / "driver.ko").write_bytes(b"excluded\n")
        (source / "nested" / "modules" / "kept.ko").write_bytes(b"kept\n")

        archive_one = self.root / "one.tar"
        manifest_one = self.root / "one.manifest.json"
        archive_two = self.root / "two.tar"
        manifest_two = self.root / "two.manifest.json"
        first = ARCHIVE.create_archive(source, archive_one, manifest_one)
        second = ARCHIVE.create_archive(source, archive_two, manifest_two)

        self.assertEqual(archive_one.read_bytes(), archive_two.read_bytes())
        self.assertEqual(manifest_one.read_bytes(), manifest_two.read_bytes())
        self.assertEqual(first, second)
        self.assertTrue(manifest_one.read_bytes().endswith(b"\n"))
        self.assertNotIn(b"\r\n", manifest_one.read_bytes())
        self.assertEqual(manifest_one.read_bytes(), canonical_json(first))
        paths = [member["path"] for member in first["members"]]
        self.assertEqual(paths, sorted(paths))
        self.assertNotIn("modules", paths)
        self.assertNotIn(".op13/config-work", paths)
        self.assertNotIn(".op13/config-work-msm-kernel", paths)
        self.assertIn("nested/modules/kept.ko", paths)
        self.assertIn(".op13/build-context.json", paths)

        destination = self.root / "restored"
        restored = ARCHIVE.restore_archive(archive_one, manifest_one, destination)
        self.assertEqual(restored, first)
        self.assertEqual((destination / ".config").read_bytes(), b"CONFIG_TEST=y\n")
        self.assertEqual(
            (destination / "nested" / "modules" / "kept.ko").read_bytes(),
            b"kept\n",
        )
        self.assertFalse((destination / "modules").exists())
        self.assertFalse((destination / ".op13" / "config-work").exists())
        self.assertTrue((destination / "empty").is_dir())
        self.assertEqual((destination / ".config").stat().st_mtime_ns, 0)
        self.assertEqual((destination / "empty").stat().st_mtime_ns, 0)
        self.assertEqual(destination.stat().st_mtime_ns, 0)

    @unittest.skipIf(os.name == "nt", "Windows source symlinks are reparse points")
    def test_symlink_created_only_after_later_regular_target(self) -> None:
        source = self.root / "source"
        source.mkdir()
        (source / "z-target").write_bytes(b"target\n")
        try:
            os.symlink("z-target", source / "a-link")
        except OSError as exc:
            self.skipTest(f"symbolic links are unavailable: {exc}")
        archive_path = self.root / "deferred.tar"
        manifest_path = self.root / "deferred.manifest.json"
        ARCHIVE.create_archive(source, archive_path, manifest_path)
        destination = self.root / "deferred-restored"
        ARCHIVE.restore_archive(archive_path, manifest_path, destination)
        self.assertTrue((destination / "a-link").is_symlink())
        self.assertEqual((destination / "a-link").read_bytes(), b"target\n")

    def test_cli_create_and_restore_uncompressed_archive(self) -> None:
        source = self.root / "source"
        source.mkdir()
        (source / "Image").write_bytes(b"image\n")
        archive_path = self.root / "kernel-build.tar"
        manifest_path = self.root / "kernel-build.manifest.json"
        create = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "create",
                "--source-dir",
                str(source),
                "--archive",
                str(archive_path),
                "--manifest",
                str(manifest_path),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(create.returncode, 0, create.stderr)
        self.assertEqual(create.stdout.encode("ascii"), manifest_path.read_bytes())

        destination = self.root / "restored"
        restore = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "restore",
                "--archive",
                str(archive_path),
                "--manifest",
                str(manifest_path),
                "--destination",
                str(destination),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(restore.returncode, 0, restore.stderr)
        self.assertEqual(restore.stdout, create.stdout)
        self.assertEqual((destination / "Image").read_bytes(), b"image\n")

    @unittest.skipUnless(shutil.which("zstd"), "zstd is not installed")
    def test_zstd_roundtrip_uses_external_compressor(self) -> None:
        source = self.root / "source"
        source.mkdir()
        (source / "Image").write_bytes(b"kernel image\n")
        archive_path = self.root / "kernel-build.tar.zst"
        manifest_path = self.root / "kernel-build.manifest.json"
        document = ARCHIVE.create_archive(source, archive_path, manifest_path)
        self.assertEqual(document["archive"]["compression"], "zstd")
        self.assertNotEqual(
            document["archive"]["sha256"], document["archive"]["tar_sha256"]
        )
        destination = self.root / "restored"
        ARCHIVE.restore_archive(archive_path, manifest_path, destination)
        self.assertEqual((destination / "Image").read_bytes(), b"kernel image\n")

    def test_zstd_frame_parser_rejects_concatenated_skippable_and_trailing_data(self) -> None:
        tar = tar_payload([{"name": "payload", "data": b"data"}])
        frame = raw_zstd_frame(tar)
        ARCHIVE._validate_single_zstd_frame(io.BytesIO(frame), len(frame), len(tar))
        invalid = (
            frame + frame,
            frame + b"trailing",
            b"\x50\x2a\x4d\x18\x00\x00\x00\x00",
        )
        for payload in invalid:
            with self.subTest(length=len(payload)):
                with self.assertRaises(ARCHIVE.ArchiveError):
                    ARCHIVE._validate_single_zstd_frame(
                        io.BytesIO(payload), len(payload), len(tar)
                    )

    def test_external_codec_deadline_kills_a_hung_process(self) -> None:
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            with mock.patch.object(ARCHIVE, "ZSTD_TIMEOUT_SECONDS", 0.05):
                with ARCHIVE._ProcessDeadline(process) as deadline:
                    process.wait(timeout=5)
                    with self.assertRaisesRegex(ARCHIVE.ArchiveError, "time limit"):
                        deadline.raise_if_expired()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    def test_manifest_archive_tar_and_member_limits_fail_before_restore(self) -> None:
        payload = tar_payload([{"name": "payload", "data": b"data"}])
        archive_path, manifest_path = self.write_fixture(
            payload, [sealed_member("payload", b"data")]
        )
        cases = (
            ("MAX_MANIFEST", manifest_path.stat().st_size - 1, "manifest exceeds"),
            ("MAX_ARCHIVE", len(payload) - 1, "archive size exceeds"),
            ("MAX_TAR", len(payload) - 1, "tar size exceeds"),
            ("MAX_MEMBER_SIZE", 3, "member exceeds"),
            ("MAX_TOTAL_FILE_BYTES", 3, "aggregate size"),
        )
        for constant, value, pattern in cases:
            destination = self.root / f"limited-{constant.lower()}"
            with self.subTest(constant=constant), mock.patch.object(
                ARCHIVE, constant, value
            ):
                with self.assertRaisesRegex(ARCHIVE.ArchiveError, pattern):
                    ARCHIVE.restore_archive(archive_path, manifest_path, destination)
                self.assertFalse(destination.exists())

    def test_ustar_member_size_boundary_is_checked_without_allocating_payload(self) -> None:
        self.assertEqual(ARCHIVE.MAX_MEMBER_SIZE, (8 * 1024 * 1024 * 1024) - 1)
        boundary = sealed_member("huge", b"")
        boundary["size"] = ARCHIVE.MAX_MEMBER_SIZE
        boundary_header = ARCHIVE._canonical_tar_header(boundary)
        self.assertEqual(len(boundary_header), tarfile.BLOCKSIZE)

        archive_digest = hashlib.sha256(b"archive").hexdigest()
        boundary_document: dict[str, object] = {
            "archive": {
                "compression": "zstd",
                "sha256": archive_digest,
                "size": len(b"archive"),
                "tar_sha256": hashlib.sha256(b"tar").hexdigest(),
                "tar_size": ARCHIVE._expected_tar_size([boundary]),
            },
            "exclusions": list(ARCHIVE.EXCLUDED_PREFIXES),
            "format": ARCHIVE.FORMAT_NAME,
            "members": [boundary],
            "version": ARCHIVE.FORMAT_VERSION,
        }
        ARCHIVE._validate_manifest_document(boundary_document)

        overflow = dict(boundary)
        overflow["size"] = ARCHIVE.MAX_MEMBER_SIZE + 1
        with self.assertRaisesRegex(ARCHIVE.ArchiveError, "canonical USTAR"):
            ARCHIVE._canonical_tar_header(overflow)
        overflow_document = dict(boundary_document)
        overflow_document["members"] = [overflow]
        overflow_archive = dict(boundary_document["archive"])
        overflow_archive["tar_size"] = ARCHIVE._expected_tar_size([overflow])
        overflow_document["archive"] = overflow_archive
        with self.assertRaisesRegex(ARCHIVE.ArchiveError, "member exceeds"):
            ARCHIVE._validate_manifest_document(overflow_document)

    def test_member_count_limit_is_checked_before_extraction(self) -> None:
        payload = tar_payload(
            [
                {"name": "first", "data": b"1"},
                {"name": "second", "data": b"2"},
            ]
        )
        archive_path, manifest_path = self.write_fixture(
            payload,
            [sealed_member("first", b"1"), sealed_member("second", b"2")],
        )
        with mock.patch.object(ARCHIVE, "MAX_MEMBER_COUNT", 1):
            with self.assertRaisesRegex(ARCHIVE.ArchiveError, "member count exceeds"):
                ARCHIVE.restore_archive(
                    archive_path, manifest_path, self.root / "count-limited"
                )

    def test_manifest_tar_size_must_be_exactly_derived_from_members(self) -> None:
        first_record = tar_payload([{"name": "payload", "data": b"data"}])
        payload = first_record + bytes(tarfile.RECORDSIZE)
        archive_path, manifest_path = self.write_fixture(
            payload, [sealed_member("payload", b"data")]
        )
        with self.assertRaisesRegex(ARCHIVE.ArchiveError, "exact canonical USTAR size"):
            ARCHIVE.restore_archive(
                archive_path, manifest_path, self.root / "wrong-tar-size"
            )

    def test_restore_requires_space_for_tar_tree_metadata_and_reserve(self) -> None:
        payload = tar_payload([{"name": "payload", "data": b"data"}])
        archive_path, manifest_path = self.write_fixture(
            payload, [sealed_member("payload", b"data")]
        )
        destination = self.root / "no-space"
        usage = mock.Mock(free=ARCHIVE.RESTORE_FREE_SPACE_RESERVE - 1)
        with mock.patch.object(ARCHIVE.shutil, "disk_usage", return_value=usage):
            with self.assertRaisesRegex(ARCHIVE.ArchiveError, "insufficient restore free space"):
                ARCHIVE.restore_archive(archive_path, manifest_path, destination)
        self.assertFalse(destination.exists())
        self.assertFalse(any(self.root.glob(".no-space.*-*")))

    def test_creation_limits_are_atomic(self) -> None:
        source = self.root / "large-source"
        source.mkdir()
        (source / "payload").write_bytes(b"four")
        archive_path = self.root / "large.tar"
        manifest_path = self.root / "large.manifest.json"
        with mock.patch.object(ARCHIVE, "MAX_MEMBER_SIZE", 3):
            with self.assertRaisesRegex(ARCHIVE.ArchiveError, "member exceeds"):
                ARCHIVE.create_archive(source, archive_path, manifest_path)
        self.assertFalse(archive_path.exists())
        self.assertFalse(manifest_path.exists())
        self.assertFalse(any(self.root.glob(".large.*tmp-*")))

    def test_rejects_parent_traversal_member(self) -> None:
        payload = tar_payload([{"name": "../escape", "data": b"bad"}])
        message = self.assert_rejected(payload, [sealed_member("payload", b"bad")])
        self.assertIn("normalized POSIX", message)
        self.assertFalse((self.root.parent / "escape").exists())

    def test_rejects_absolute_member(self) -> None:
        payload = tar_payload([{"name": "/escape", "data": b"bad"}])
        message = self.assert_rejected(payload, [sealed_member("payload", b"bad")])
        self.assertIn("normalized POSIX", message)

    def test_rejects_symbolic_link_member(self) -> None:
        payload = tar_payload(
            [{"name": "payload", "type": tarfile.SYMTYPE, "linkname": "target"}]
        )
        message = self.assert_rejected(payload, [sealed_member("payload")])
        self.assertIn("type mismatch", message)

    def test_rejects_hard_link_member(self) -> None:
        payload = tar_payload(
            [{"name": "payload", "type": tarfile.LNKTYPE, "linkname": "target"}]
        )
        message = self.assert_rejected(payload, [sealed_member("payload")])
        self.assertIn("type mismatch", message)

    def test_rejects_unsafe_missing_and_cyclic_symlink_graphs(self) -> None:
        cases = (
            (
                "absolute",
                [{"name": "link", "type": tarfile.SYMTYPE, "linkname": "/outside"}],
                [sealed_member("link", kind="symlink", target="/outside")],
                "normalized relative",
            ),
            (
                "escape",
                [{"name": "link", "type": tarfile.SYMTYPE, "linkname": "../outside"}],
                [sealed_member("link", kind="symlink", target="../outside")],
                "escapes the archive root",
            ),
            (
                "noncanonical",
                [
                    {
                        "name": "link",
                        "type": tarfile.SYMTYPE,
                        "linkname": "directory/../target",
                    }
                ],
                [
                    sealed_member(
                        "link",
                        kind="symlink",
                        target="directory/../target",
                    )
                ],
                "normalized relative",
            ),
            (
                "missing",
                [{"name": "link", "type": tarfile.SYMTYPE, "linkname": "missing"}],
                [sealed_member("link", kind="symlink", target="missing")],
                "target is missing",
            ),
            (
                "cycle",
                [
                    {"name": "a", "type": tarfile.SYMTYPE, "linkname": "b"},
                    {"name": "b", "type": tarfile.SYMTYPE, "linkname": "a"},
                ],
                [
                    sealed_member("a", kind="symlink", target="b"),
                    sealed_member("b", kind="symlink", target="a"),
                ],
                "cycle is forbidden",
            ),
        )
        for label, entries, members, pattern in cases:
            with self.subTest(label=label):
                archive_path, manifest_path = self.write_fixture(
                    tar_payload(entries), members, name=f"{label}.tar"
                )
                destination = self.root / f"{label}-destination"
                with self.assertRaisesRegex(ARCHIVE.ArchiveError, pattern):
                    ARCHIVE.restore_archive(archive_path, manifest_path, destination)
                self.assertFalse(destination.exists())

    @unittest.skipIf(os.name == "nt", "Windows symlink creation may require elevation")
    def test_sealed_symlink_chain_resolves_to_archived_regular_file(self) -> None:
        payload = tar_payload(
            [
                {"name": "a-link", "type": tarfile.SYMTYPE, "linkname": "b-link"},
                {"name": "b-link", "type": tarfile.SYMTYPE, "linkname": "z-target"},
                {"name": "z-target", "data": b"target"},
            ]
        )
        members = [
            sealed_member("a-link", kind="symlink", target="b-link"),
            sealed_member("b-link", kind="symlink", target="z-target"),
            sealed_member("z-target", b"target"),
        ]
        archive_path, manifest_path = self.write_fixture(
            payload, members, name="chain.tar"
        )
        destination = self.root / "chain-restored"
        ARCHIVE.restore_archive(archive_path, manifest_path, destination)
        self.assertEqual((destination / "a-link").read_bytes(), b"target")
        self.assertEqual(os.readlink(destination / "a-link"), "b-link")

    def test_long_symlink_chain_is_iterative_memoized_and_cycle_safe(self) -> None:
        link_count = 1500
        links = [f"link-{index:04d}" for index in range(link_count)]
        members = [
            sealed_member(
                path,
                kind="symlink",
                target=links[index + 1] if index + 1 < link_count else "terminal",
            )
            for index, path in enumerate(links)
        ]
        members.append(sealed_member("terminal", b"target"))
        resolved = ARCHIVE._validate_symlink_graph(members)
        self.assertEqual(len(resolved), link_count)
        self.assertEqual(set(resolved.values()), {"file"})

        cyclic = [dict(member) for member in members]
        cyclic[link_count - 1]["target"] = links[0]
        with self.assertRaisesRegex(ARCHIVE.ArchiveError, "cycle is forbidden"):
            ARCHIVE._validate_symlink_graph(cyclic)

    def test_rejects_fifo_and_device_members(self) -> None:
        cases = (
            ("fifo", tarfile.FIFOTYPE, {}),
            ("character device", tarfile.CHRTYPE, {"devmajor": 1, "devminor": 3}),
            ("block device", tarfile.BLKTYPE, {"devmajor": 8, "devminor": 0}),
        )
        for label, member_type, extras in cases:
            with self.subTest(label=label):
                payload = tar_payload(
                    [{"name": "payload", "type": member_type, **extras}]
                )
                message = self.assert_rejected(payload, [sealed_member("payload")])
                self.assertIn("type mismatch", message)
                (self.root / "kernel-build.tar").unlink()
                (self.root / "kernel-build.tar.manifest.json").unlink()

    def test_rejects_unknown_special_member(self) -> None:
        payload = tar_payload([{"name": "payload", "type": tarfile.CONTTYPE}])
        message = self.assert_rejected(payload, [sealed_member("payload")])
        self.assertIn("type mismatch", message)

    def test_rejects_v7_and_gnu_headers_even_when_tarfile_accepts_them(self) -> None:
        canonical = tar_payload([{"name": "payload", "data": b"data"}])
        variants: list[tuple[str, bytes]] = []

        v7 = bytearray(canonical)
        v7[257:265] = bytes(8)
        rewrite_tar_checksum(v7)
        variants.append(("V7", bytes(v7)))

        gnu = bytearray(canonical)
        gnu[257:265] = b"ustar  \0"
        rewrite_tar_checksum(gnu)
        variants.append(("GNU", bytes(gnu)))

        for label, payload in variants:
            with self.subTest(label=label):
                message = self.assert_rejected(
                    payload, [sealed_member("payload", b"data")]
                )
                self.assertIn("not canonical USTAR", message)
                (self.root / "kernel-build.tar").unlink()
                (self.root / "kernel-build.tar.manifest.json").unlink()

    def test_rejects_alternate_base256_numeric_header(self) -> None:
        payload = bytearray(tar_payload([{"name": "payload", "data": b"data"}]))
        payload[100:108] = (0o644 | (1 << 63)).to_bytes(8, "big")
        rewrite_tar_checksum(payload)
        message = self.assert_rejected(
            bytes(payload), [sealed_member("payload", b"data")]
        )
        self.assertIn("not canonical USTAR", message)

    def test_rejects_duplicate_member(self) -> None:
        payload = tar_payload(
            [
                {"name": "payload", "data": b"same"},
                {"name": "payload", "data": b"same"},
            ]
        )
        message = self.assert_rejected(payload, [sealed_member("payload", b"same")])
        self.assertIn("duplicate tar member", message)

    def test_rejects_extra_member(self) -> None:
        payload = tar_payload(
            [
                {"name": "payload", "data": b"kept"},
                {"name": "unrecorded", "data": b"extra"},
            ]
        )
        message = self.assert_rejected(payload, [sealed_member("payload", b"kept")])
        self.assertIn("unexpected tar member", message)

    def test_rejects_missing_member(self) -> None:
        payload = tar_payload([])
        message = self.assert_rejected(payload, [sealed_member("payload", b"")])
        self.assertIn("member is missing", message)

    def test_rejects_digest_mismatch(self) -> None:
        payload = tar_payload([{"name": "payload", "data": b"tampered"}])
        message = self.assert_rejected(
            payload,
            [sealed_member("payload", b"tampered", digest_data=b"expected")],
        )
        self.assertIn("digest mismatch", message)

    def test_rejects_mode_change(self) -> None:
        payload = tar_payload([{"name": "payload", "data": b"data", "mode": 0o644}])
        message = self.assert_rejected(
            payload, [sealed_member("payload", b"data", mode=0o600)]
        )
        self.assertIn("mode mismatch", message)

    def test_rejects_size_change(self) -> None:
        payload = tar_payload([{"name": "payload", "data": b"data"}])
        message = self.assert_rejected(payload, [sealed_member("payload", b"longer")])
        self.assertIn("size mismatch", message)

    def test_rejects_type_change(self) -> None:
        payload = tar_payload([{"name": "payload", "data": b""}])
        message = self.assert_rejected(
            payload, [sealed_member("payload", kind="directory")]
        )
        self.assertIn("type mismatch", message)

    def test_rejects_trailing_unrecorded_payload(self) -> None:
        payload = bytearray(tar_payload([{"name": "payload", "data": b"data"}]))
        payload[2048:2056] = b"trailing"
        payload = bytes(payload)
        message = self.assert_rejected(payload, [sealed_member("payload", b"data")])
        self.assertIn("non-zero end padding", message)

    def test_rejects_noncanonical_or_unknown_manifest_fields(self) -> None:
        payload = tar_payload([{"name": "payload", "data": b"data"}])
        archive_path, manifest_path = self.write_fixture(
            payload, [sealed_member("payload", b"data")]
        )
        document = json.loads(manifest_path.read_text(encoding="ascii"))
        document["unexpected"] = True
        manifest_path.write_bytes(canonical_json(document))
        with self.assertRaisesRegex(ARCHIVE.ArchiveError, "keys do not match schema"):
            ARCHIVE.restore_archive(
                archive_path, manifest_path, self.root / "unexpected-destination"
            )

        document.pop("unexpected")
        manifest_path.write_text(json.dumps(document, indent=2), encoding="ascii")
        with self.assertRaisesRegex(ARCHIVE.ArchiveError, "not in canonical encoding"):
            ARCHIVE.restore_archive(
                archive_path, manifest_path, self.root / "pretty-destination"
            )

    def test_creation_rejects_hardlinks(self) -> None:
        source = self.root / "source"
        source.mkdir()
        original = source / "original"
        original.write_bytes(b"data")
        linked = source / "linked"
        try:
            os.link(original, linked)
        except OSError as exc:
            self.skipTest(f"hard links are unavailable: {exc}")
        with self.assertRaisesRegex(ARCHIVE.ArchiveError, "hard-linked"):
            ARCHIVE.create_archive(
                source,
                self.root / "hardlink.tar",
                self.root / "hardlink.manifest.json",
            )

    def test_relative_in_tree_symlink_roundtrip(self) -> None:
        if os.name == "nt":
            self.skipTest("Windows reparse-point sources are deliberately rejected")
        source = self.root / "source"
        (source / "real").mkdir(parents=True)
        (source / "real" / "Image").write_bytes(b"image\n")
        linked = source / "Image-link"
        try:
            os.symlink("real/Image", linked)
        except OSError as exc:
            self.skipTest(f"symbolic links are unavailable: {exc}")
        archive_path = self.root / "symlink.tar"
        manifest_path = self.root / "symlink.manifest.json"
        document = ARCHIVE.create_archive(source, archive_path, manifest_path)
        member = next(item for item in document["members"] if item["path"] == "Image-link")
        self.assertEqual(member["type"], "symlink")
        self.assertEqual(member["target"], "real/Image")
        destination = self.root / "symlink-restored"
        ARCHIVE.restore_archive(archive_path, manifest_path, destination)
        self.assertTrue((destination / "Image-link").is_symlink())
        self.assertEqual(os.readlink(destination / "Image-link"), "real/Image")
        self.assertEqual((destination / "Image-link").read_bytes(), b"image\n")

    def test_creation_rejects_unsafe_source_name(self) -> None:
        if os.name == "nt":
            self.skipTest("Windows does not permit the unsafe fixture name")
        source = self.root / "source"
        source.mkdir()
        (source / "line\nbreak").write_bytes(b"data")
        with self.assertRaisesRegex(ARCHIVE.ArchiveError, "control character"):
            ARCHIVE.create_archive(
                source,
                self.root / "unsafe.tar",
                self.root / "unsafe.manifest.json",
            )

    @unittest.skipIf(os.name == "nt", "covered by the Windows junction test")
    def test_source_and_output_paths_must_not_traverse_symlink_parents(self) -> None:
        real_source = self.root / "real-source"
        real_source.mkdir()
        (real_source / "payload").write_bytes(b"data")
        source_link = self.root / "source-link"
        output_dir = self.root / "real-output"
        output_dir.mkdir()
        output_link = self.root / "output-link"
        try:
            os.symlink(real_source.name, source_link, target_is_directory=True)
            os.symlink(output_dir.name, output_link, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlinks are unavailable: {exc}")
        with self.assertRaisesRegex(ARCHIVE.ArchiveError, "traverse a symbolic link"):
            ARCHIVE.create_archive(
                source_link,
                self.root / "linked-source.tar",
                self.root / "linked-source.manifest.json",
            )
        with self.assertRaisesRegex(ARCHIVE.ArchiveError, "traverse a symbolic link"):
            ARCHIVE.create_archive(
                real_source,
                output_link / "payload.tar",
                output_link / "payload.manifest.json",
            )

    @unittest.skipUnless(os.name == "nt", "NTFS junction coverage")
    def test_windows_junction_source_child_is_rejected_without_traversal(self) -> None:
        source = self.root / "junction-source"
        outside = self.root / "outside"
        source.mkdir()
        outside.mkdir()
        (outside / "must-not-be-archived").write_bytes(b"outside")
        junction = source / "junction"
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
            check=False,
            capture_output=True,
            text=True,
        )
        if created.returncode != 0:
            self.skipTest(f"junction creation is unavailable: {created.stderr}")
        try:
            with self.assertRaisesRegex(ARCHIVE.ArchiveError, "junction or reparse"):
                ARCHIVE.create_archive(
                    source,
                    self.root / "junction.tar",
                    self.root / "junction.manifest.json",
                )
            self.assertFalse((self.root / "junction.tar").exists())
        finally:
            junction.rmdir()

        output_real = self.root / "output-real"
        output_real.mkdir()
        output_junction = self.root / "output-junction"
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(output_junction), str(output_real)],
            check=False,
            capture_output=True,
            text=True,
        )
        if created.returncode != 0:
            self.skipTest(f"output junction creation is unavailable: {created.stderr}")
        (source / "regular").write_bytes(b"data")
        try:
            with self.assertRaisesRegex(ARCHIVE.ArchiveError, "junction or reparse"):
                ARCHIVE.create_archive(
                    source,
                    output_junction / "artifact.tar",
                    output_junction / "artifact.manifest.json",
                )
            archive_path = self.root / "valid.tar"
            manifest_path = self.root / "valid.manifest.json"
            ARCHIVE.create_archive(source, archive_path, manifest_path)
            with self.assertRaisesRegex(ARCHIVE.ArchiveError, "junction or reparse"):
                ARCHIVE.restore_archive(
                    archive_path,
                    manifest_path,
                    output_junction / "restored",
                )
        finally:
            output_junction.rmdir()

    def test_creation_rejects_special_permission_bits(self) -> None:
        if os.name == "nt":
            self.skipTest("Windows does not preserve special POSIX permission bits")
        source = self.root / "source"
        source.mkdir()
        path = source / "setuid"
        path.write_bytes(b"data")
        path.chmod(0o4755)
        with self.assertRaisesRegex(ARCHIVE.ArchiveError, "special permission"):
            ARCHIVE.create_archive(
                source,
                self.root / "special-mode.tar",
                self.root / "special-mode.manifest.json",
            )

    def test_restoration_is_atomic_on_digest_failure(self) -> None:
        payload = tar_payload(
            [
                {"name": "first", "data": b"verified"},
                {"name": "second", "data": b"tampered"},
            ]
        )
        members = [
            sealed_member("first", b"verified"),
            sealed_member("second", b"tampered", digest_data=b"expected"),
        ]
        message = self.assert_rejected(payload, members)
        self.assertIn("digest mismatch", message)


if __name__ == "__main__":
    unittest.main()
