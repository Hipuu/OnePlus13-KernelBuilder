from __future__ import annotations

import hashlib
import json
import stat
import sys
import tempfile
import unittest
import zipfile
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lib.artifacts import (
    ANYKERNEL_EXECUTABLE_PROVENANCE,
    ANYKERNEL_LICENSE_MEMBERS,
    ANYKERNEL_SOURCE_CONVEYANCE,
    ANYKERNEL_TOOL_CONTRACTS,
    ANYKERNEL_UPSTREAM_MEMBER_CONTRACTS,
    ANYKERNEL_UPSTREAM_MEMBERS,
    ANYKERNEL_ZIP_MODES,
    _anykernel_tree_records,
    _prepare_anykernel_tree,
    _validate_anykernel_script,
    _verify_anykernel_zip,
    deterministic_zip,
)
from lib.config import Dependency, load_dependency_lock, sha256_file
from lib.errors import BuildToolError


ANYKERNEL_COMMIT = "1" * 40
MAGISK_COMMIT = "2" * 40
BUSYBOX_COMMIT = "3" * 40
ASSET_URI = "https://example.com/releases/download/v1/Magisk-v1.apk"


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _elf64(*, machine: int = 183, elf_class: int = 2, marker: bytes) -> bytes:
    header = bytearray(64)
    header[:4] = b"\x7fELF"
    header[4] = elf_class
    header[5] = 1
    header[6] = 1
    header[16:18] = (2).to_bytes(2, "little")
    header[18:20] = machine.to_bytes(2, "little")
    return bytes(header) + marker


def _valid_script() -> str:
    return """#!/sbin/sh
properties() { '
kernel.string=OnePlus 13 test
do.devicecheck=1
do.modules=0
do.systemless=0
do.cleanup=1
do.cleanuponabort=0
device.name1=dodge
supported.versions=
supported.patchlevels=
supported.vendorpatchlevels=
'; }
BLOCK=boot;
IS_SLOT_DEVICE=1;
RAMDISK_COMPRESSION=auto;
PATCH_VBMETA_FLAG=auto;
. tools/ak3-core.sh;
split_boot;
flash_boot;
"""


class AnyKernelPackagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.overlay = self.root / "packaging" / "anykernel3"
        self.overlay.mkdir(parents=True)
        self.upstream = {
            "LICENSE": b"upstream AnyKernel license\r\n",
            "META-INF/com/google/android/update-binary": b"#!/sbin/sh\r\n",
            "META-INF/com/google/android/updater-script": b"#MAGISK\r\n",
            "tools/ak3-core.sh": b"#!/sbin/sh\r\n# source-visible helper\r\n",
        }
        for member, content in self.upstream.items():
            path = self.source.joinpath(*member.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        (self.source / "tools" / "busybox").write_bytes(b"\x7fELF32-untrusted")
        (self.source / "tools" / "magiskboot").write_bytes(b"\x7fELF32-untrusted")
        (self.source / "tools" / "fec").write_bytes(b"\x7fELFunallowlisted")
        (self.overlay / "anykernel.sh").write_text(
            _valid_script(), encoding="utf-8", newline="\n"
        )
        (self.overlay / ANYKERNEL_SOURCE_CONVEYANCE).write_text(
            "Synthetic exact source locations.\n", encoding="utf-8", newline="\n"
        )
        for output, source_member in ANYKERNEL_LICENSE_MEMBERS.items():
            path = self.overlay.joinpath(*source_member.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"fixture {output} license\n", encoding="utf-8", newline="\n")

        self.busybox = _elf64(marker=b"synthetic-busybox")
        self.magiskboot = _elf64(marker=b"synthetic-magiskboot")
        self.asset = self.root / "Magisk-v1.apk"
        self._write_asset()
        self.anykernel_dependency = Dependency(
            id="anykernel3",
            kind="git",
            url="https://example.com/AnyKernel3.git",
            commit=ANYKERNEL_COMMIT,
            ref=ANYKERNEL_COMMIT,
            sha256=None,
            required_for=("package-anykernel3",),
            raw={"license": "SEE-UPSTREAM-MULTIPLE"},
        )
        self.magisk_dependency = self._magisk_dependency()
        self.tool_contracts = self._tool_contracts()
        self.upstream_contracts = tuple(
            {
                "path": member,
                "git_mode": (
                    "100755" if ANYKERNEL_ZIP_MODES[member] == 0o755 else "100644"
                ),
                "git_blob": hashlib.sha1(
                    b"blob "
                    + str(len(payload)).encode("ascii")
                    + b"\0"
                    + payload,
                    usedforsecurity=False,
                ).hexdigest(),
                "size": len(payload),
                "sha256": _sha256(payload),
            }
            for member in ANYKERNEL_UPSTREAM_MEMBERS
            for payload in (self.upstream[member].replace(b"\r\n", b"\n"),)
        )
        self._write_provenance()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_asset(self, *, busybox: bytes | None = None) -> None:
        with zipfile.ZipFile(self.asset, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("lib/arm64-v8a/libbusybox.so", self.busybox if busybox is None else busybox)
            archive.writestr("lib/arm64-v8a/libmagiskboot.so", self.magiskboot)
            archive.writestr("lib/armeabi-v7a/libbusybox.so", b"\x7fELF32-never-selected")

    def _magisk_dependency(self) -> Dependency:
        return Dependency(
            id="magisk_release_apk",
            kind="release_asset",
            url=ASSET_URI,
            commit=MAGISK_COMMIT,
            ref="refs/tags/v1",
            sha256=sha256_file(self.asset),
            required_for=("package-anykernel3",),
            raw={
                "repository": "https://example.com/Magisk.git",
                "version": "v1",
                "license": "SEE-UPSTREAM-MULTIPLE",
            },
        )

    def _tool_contracts(self) -> dict[str, dict[str, object]]:
        return {
            "tools/busybox": {
                "archive_member": "lib/arm64-v8a/libbusybox.so",
                "size": len(self.busybox),
                "sha256": _sha256(self.busybox),
                "version": "fixture-busybox",
                "license": "GPL-2.0-only",
                "license_path": "LICENSES/GPL-2.0-only",
                "source": {
                    "repository": "https://example.com/ndk-busybox.git",
                    "commit": BUSYBOX_COMMIT,
                    "relationship": "official-version-source-exact-byte-rebuild-not-verified",
                },
                "upstream_build_input": {
                    "uri": "https://example.com/busybox.zip",
                    "sha256": "4" * 64,
                    "archive_member": "arm64-v8a/libbusybox.so",
                },
            },
            "tools/magiskboot": {
                "archive_member": "lib/arm64-v8a/libmagiskboot.so",
                "size": len(self.magiskboot),
                "sha256": _sha256(self.magiskboot),
                "version": "fixture-magiskboot",
                "license": "GPL-3.0-or-later",
                "license_path": "LICENSES/GPL-3.0-or-later",
                "source": {
                    "repository": "https://example.com/Magisk.git",
                    "commit": MAGISK_COMMIT,
                    "relationship": "official-release-source-exact-byte-rebuild-not-verified",
                },
            },
        }

    def _write_provenance(self, *, anykernel_commit: str = ANYKERNEL_COMMIT) -> None:
        license_records = []
        for output, source_member in sorted(ANYKERNEL_LICENSE_MEMBERS.items()):
            path = self.overlay.joinpath(*source_member.split("/"))
            spdx = output.split("/")[-1]
            source_contract = (
                self.tool_contracts["tools/busybox"]
                if spdx == "GPL-2.0-only"
                else self.tool_contracts["tools/magiskboot"]
            )
            license_records.append(
                {
                    "path": output,
                    "size": path.stat().st_size,
                    "sha256": sha256_file(path),
                    "spdx": spdx,
                    "source": {
                        "repository": source_contract["source"]["repository"],
                        "commit": source_contract["source"]["commit"],
                        "path": "LICENSE",
                    },
                }
            )
        executable_records = []
        for member, contract in self.tool_contracts.items():
            origin = {
                "dependency": "magisk_release_apk",
                "archive_member": contract["archive_member"],
                "asset_sha256": self.magisk_dependency.sha256,
            }
            if "upstream_build_input" in contract:
                origin["upstream_build_input"] = contract["upstream_build_input"]
            executable_records.append(
                {
                    "path": member,
                    "size": contract["size"],
                    "sha256": contract["sha256"],
                    "version": contract["version"],
                    "license": contract["license"],
                    "license_path": contract["license_path"],
                    "elf": {
                        "class": "ELFCLASS64",
                        "data": "ELFDATA2LSB",
                        "machine": "EM_AARCH64",
                        "type": "ET_EXEC",
                    },
                    "source": contract["source"],
                    "binary_origin": origin,
                    "reproducible_build": {
                        "status": "official-release-match-byte-rebuild-not-verified",
                        "note": "fixture records an honest unverified rebuild status",
                    },
                }
            )
        conveyance = self.overlay / ANYKERNEL_SOURCE_CONVEYANCE
        document = {
            "schema_version": 2,
            "policy": "synthetic exact origin fixture",
            "anykernel3": {
                "dependency": "anykernel3",
                "repository": self.anykernel_dependency.url,
                "commit": anykernel_commit,
                "license_classification": "SEE-UPSTREAM-MULTIPLE",
                "license_member": "LICENSE",
                "template_members": [
                    dict(record) for record in self.upstream_contracts
                ],
            },
            "release_asset": {
                "dependency": "magisk_release_apk",
                "uri": ASSET_URI,
                "sha256": self.magisk_dependency.sha256,
                "repository": "https://example.com/Magisk.git",
                "ref": "refs/tags/v1",
                "source_commit": MAGISK_COMMIT,
                "version": "v1",
                "license_classification": "SEE-UPSTREAM-MULTIPLE",
                "archive_format": "apk-zip",
                "abi": "arm64-v8a",
            },
            "license_files": license_records,
            "source_conveyance": {
                "path": ANYKERNEL_SOURCE_CONVEYANCE,
                "size": conveyance.stat().st_size,
                "sha256": sha256_file(conveyance),
            },
            "executables": executable_records,
        }
        (self.overlay / ANYKERNEL_EXECUTABLE_PROVENANCE).write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    def _prepare(self, destination: Path | None = None) -> tuple[Path, dict[str, object]]:
        destination = self.root / "package" if destination is None else destination
        provenance = _prepare_anykernel_tree(
            root=self.root,
            source=self.source,
            magisk_asset=self.asset,
            destination=destination,
            anykernel_dependency=self.anykernel_dependency,
            magisk_dependency=self.magisk_dependency,
            tool_contracts=self.tool_contracts,
            upstream_contracts=self.upstream_contracts,
        )
        return destination, provenance

    def test_extracts_only_arm64_tools_and_minimal_allowlist(self) -> None:
        destination, provenance = self._prepare()
        actual = {
            path.relative_to(destination).as_posix()
            for path in destination.rglob("*")
            if path.is_file()
        }
        self.assertEqual(
            actual,
            set(ANYKERNEL_UPSTREAM_MEMBERS)
            | set(self.tool_contracts)
            | set(ANYKERNEL_LICENSE_MEMBERS)
            | {"anykernel.sh", ANYKERNEL_EXECUTABLE_PROVENANCE, ANYKERNEL_SOURCE_CONVEYANCE},
        )
        self.assertEqual((destination / "tools" / "busybox").read_bytes(), self.busybox)
        self.assertFalse((destination / "tools" / "fec").exists())
        self.assertNotIn(b"ELF32", (destination / "tools" / "busybox").read_bytes())
        self.assertEqual(provenance["release_asset"]["abi"], "arm64-v8a")
        self.assertNotIn(b"\r", (destination / "META-INF/com/google/android/update-binary").read_bytes())

    def test_exact_binary_digest_mismatch_fails_closed(self) -> None:
        changed = _elf64(marker=b"changed-busybox")
        self._write_asset(busybox=changed)
        self.magisk_dependency = self._magisk_dependency()
        self._write_provenance()
        with self.assertRaisesRegex(BuildToolError, "exact arm64 contract"):
            self._prepare()

    def test_elfclass32_or_em_arm_fails_closed(self) -> None:
        arm32 = _elf64(machine=40, elf_class=1, marker=b"arm32")
        self._write_asset(busybox=arm32)
        self.magisk_dependency = self._magisk_dependency()
        changed = deepcopy(self.tool_contracts)
        changed["tools/busybox"]["size"] = len(arm32)
        changed["tools/busybox"]["sha256"] = _sha256(arm32)
        self.tool_contracts = changed
        self._write_provenance()
        with self.assertRaisesRegex(BuildToolError, "ELFCLASS64"):
            self._prepare()

    def test_dependency_commit_mismatch_fails_closed(self) -> None:
        self._write_provenance(anykernel_commit="9" * 40)
        with self.assertRaisesRegex(BuildToolError, "differs from dependencies/lock.yml"):
            self._prepare()

    def test_executable_policy_rejects_duplicate_object_keys(self) -> None:
        path = self.overlay / ANYKERNEL_EXECUTABLE_PROVENANCE
        text = path.read_text(encoding="utf-8")
        path.write_text(
            text.replace(
                '"schema_version": 2,',
                '"schema_version": 2,\n  "schema_version": 2,',
                1,
            ),
            encoding="utf-8",
            newline="\n",
        )
        with self.assertRaisesRegex(BuildToolError, "manifest is invalid"):
            self._prepare()

    def test_upstream_template_mutation_fails_the_pinned_git_blob_contract(self) -> None:
        (self.source / "tools" / "ak3-core.sh").write_bytes(
            b"#!/sbin/sh\n# resealed template mutation\n"
        )
        with self.assertRaisesRegex(BuildToolError, "pinned Git blob"):
            self._prepare()

    def test_final_zip_has_exact_members_and_host_independent_modes(self) -> None:
        destination, _ = self._prepare()
        (destination / "Image").write_bytes(b"synthetic kernel image")
        records = _anykernel_tree_records(destination)
        archive = self.root / "AnyKernel3.zip"
        deterministic_zip(
            destination,
            archive,
            epoch=1783987200,
            member_modes=ANYKERNEL_ZIP_MODES,
        )
        _verify_anykernel_zip(archive, records)
        with zipfile.ZipFile(archive, "r") as package:
            modes = {
                info.filename: stat.S_IMODE((info.external_attr >> 16) & 0xFFFF)
                for info in package.infolist()
                if not info.is_dir()
            }
        self.assertEqual(modes, dict(ANYKERNEL_ZIP_MODES))

        wrong_modes = dict(ANYKERNEL_ZIP_MODES)
        wrong_modes["tools/busybox"] = 0o644
        bad_archive = self.root / "AnyKernel3-bad-mode.zip"
        deterministic_zip(
            destination,
            bad_archive,
            epoch=1783987200,
            member_modes=wrong_modes,
        )
        with self.assertRaisesRegex(BuildToolError, "mode differs"):
            _verify_anykernel_zip(bad_archive, records)

    def test_device_script_rejects_inherited_template_mutation(self) -> None:
        bad = self.root / "bad-anykernel.sh"
        bad.write_text(
            _valid_script() + "dump_boot;\nwrite_boot;\n# tuna\n",
            encoding="utf-8",
            newline="\n",
        )
        with self.assertRaisesRegex(BuildToolError, "forbidden template operation"):
            _validate_anykernel_script(bad)

    def test_repository_provenance_contract_is_exact_v30_7_arm64(self) -> None:
        provenance = json.loads(
            (ROOT / "packaging" / "anykernel3" / ANYKERNEL_EXECUTABLE_PROVENANCE).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(provenance["schema_version"], 2)
        self.assertEqual(
            provenance["anykernel3"]["template_members"],
            [dict(record) for record in ANYKERNEL_UPSTREAM_MEMBER_CONTRACTS],
        )
        self.assertEqual(provenance["release_asset"]["abi"], "arm64-v8a")
        self.assertEqual(
            provenance["release_asset"]["license_classification"],
            "SEE-UPSTREAM-MULTIPLE",
        )
        self.assertEqual(
            provenance["release_asset"]["sha256"],
            "e0d32d2123532860f97123d927b1bb86c4e08e6fd8a48bfc6b5bee0afae9ebd5",
        )
        records = {record["path"]: record for record in provenance["executables"]}
        self.assertEqual(records["tools/busybox"]["size"], 1710600)
        self.assertEqual(
            records["tools/busybox"]["sha256"],
            "4d60ab3f5a59ebb2ca863f2f514e6924401b581e9b64f602665c008177626651",
        )
        self.assertEqual(records["tools/magiskboot"]["size"], 788840)
        self.assertEqual(
            records["tools/magiskboot"]["sha256"],
            "d7440e2cd89899426e809554bf793baef9804ccbe5a52ce34a8b6242725d3c77",
        )
        self.assertTrue(
            all(record["elf"]["machine"] == "EM_AARCH64" for record in records.values())
        )
        self.assertEqual(set(records), set(ANYKERNEL_TOOL_CONTRACTS))

    def test_real_cached_magisk_asset_extracts_exact_arm64_members(self) -> None:
        cache = ROOT / ".cache" / "op13" / "files"
        candidates = sorted(cache.glob("magisk_release_apk-e0d32d212353*.apk"))
        anykernel_source = ROOT / ".cache" / "op13" / "git" / "anykernel3"
        if not candidates or not anykernel_source.is_dir():
            self.skipTest("real pinned AnyKernel dependencies are not cached")
        lock = load_dependency_lock(ROOT / "dependencies" / "lock.yml")
        destination = self.root / "real-package"
        _prepare_anykernel_tree(
            root=ROOT,
            source=anykernel_source,
            magisk_asset=candidates[0],
            destination=destination,
            anykernel_dependency=lock.dependencies["anykernel3"],
            magisk_dependency=lock.dependencies["magisk_release_apk"],
        )
        self.assertEqual(
            sha256_file(destination / "tools" / "busybox"),
            "4d60ab3f5a59ebb2ca863f2f514e6924401b581e9b64f602665c008177626651",
        )
        self.assertEqual(
            sha256_file(destination / "tools" / "magiskboot"),
            "d7440e2cd89899426e809554bf793baef9804ccbe5a52ce34a8b6242725d3c77",
        )


if __name__ == "__main__":
    unittest.main()
