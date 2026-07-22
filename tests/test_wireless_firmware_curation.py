from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
import zipfile
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lib.artifacts import (
    WIRELESS_FIRMWARE_LICENSE,
    WIRELESS_FIRMWARE_LOCK_LICENSE,
    WIRELESS_FIRMWARE_PROVENANCE,
    WIRELESS_FIRMWARE_PROVENANCE_STATUS,
    WIRELESS_FIRMWARE_REQUIRED_GAP_IDS,
    _curate_wireless_firmware_tree,
    _load_wireless_firmware_policy,
    _wireless_authoritative_repository_path,
    _verify_curated_wireless_firmware_zip,
    deterministic_zip,
)
from lib.config import Dependency, load_dependency_lock, sha256_file
from lib.errors import BuildToolError


COMMIT = "1" * 40
ASSET_URI = "https://example.com/releases/download/v1/firmware.zip"
REPOSITORY = "https://example.com/firmware.git"
RELEASE_REF = "refs/tags/v1"
VERSION = "v1"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class WirelessFirmwareCurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        packaging = self.root / "packaging" / "wireless-firmware"
        packaging.mkdir(parents=True)
        (packaging / "licenses").mkdir()
        (packaging / "README.md").write_text(
            "Synthetic curation fixture.\n",
            encoding="utf-8",
            newline="\n",
        )
        (packaging / "WHENCE").write_text(
            "Synthetic attribution fixture.\n",
            encoding="utf-8",
            newline="\n",
        )
        self.license_payload = b"fixture MediaTek license\n"
        (packaging / "licenses" / "LICENCE.fixture").write_bytes(
            self.license_payload
        )
        self.source = self.root / "source.zip"
        self.payloads = {
            "LICENSE.md": b"fixture license\n",
            "README.md": b"fixture readme\n",
            "system/etc/firmware/mediatek/fixture.bin": b"opaque firmware\n",
            "system/xbin/excluded-elf": b"\x7fELFexcluded\n",
        }
        self._write_source()
        self.dependency = self._dependency()
        self.rtw88_dependency = Dependency(
            id="rtw88",
            kind="git",
            url="https://example.com/rtw88.git",
            commit="3" * 40,
            ref="3" * 40,
            sha256=None,
            required_for=("nethunter", "modules"),
            raw={"license": "GPL-2.0-only"},
        )
        self.policy = self._policy()
        self._write_policy()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_source(self) -> None:
        with zipfile.ZipFile(
            self.source,
            "w",
            compression=zipfile.ZIP_DEFLATED,
        ) as archive:
            for path, data in sorted(self.payloads.items()):
                archive.writestr(path, data)

    def _dependency(self) -> Dependency:
        return Dependency(
            id="nethunter_wireless_firmware",
            kind="release_asset",
            url=ASSET_URI,
            commit=COMMIT,
            ref=RELEASE_REF,
            sha256=sha256_file(self.source),
            required_for=("nethunter", "release"),
            raw={
                "repository": REPOSITORY,
                "version": VERSION,
                "license": WIRELESS_FIRMWARE_LOCK_LICENSE,
            },
        )

    def _policy(self) -> dict[str, object]:
        source = {
            "dependency": "nethunter_wireless_firmware",
            "asset_uri": ASSET_URI,
            "asset_sha256": self.dependency.sha256,
            "repository": REPOSITORY,
            "release_ref": RELEASE_REF,
            "release_commit": COMMIT,
            "release_version": VERSION,
            "license_classification": WIRELESS_FIRMWARE_LOCK_LICENSE,
        }
        members = [
            {
                "path": "LICENSE.md",
                "size": len(self.payloads["LICENSE.md"]),
                "sha256": _digest(self.payloads["LICENSE.md"]),
                "kind": "attribution",
                "family": "attribution",
                "license": "MIT-license-text-upstream-package",
                "output_path": "UPSTREAM-PACKAGE-LICENSE.md",
            },
            {
                "path": "README.md",
                "size": len(self.payloads["README.md"]),
                "sha256": _digest(self.payloads["README.md"]),
                "kind": "attribution",
                "family": "attribution",
                "license": "SEE-UPSTREAM",
                "output_path": "UPSTREAM-README.md",
            },
            {
                "path": "system/etc/firmware/mediatek/fixture.bin",
                "size": len(
                    self.payloads["system/etc/firmware/mediatek/fixture.bin"]
                ),
                "sha256": _digest(
                    self.payloads["system/etc/firmware/mediatek/fixture.bin"]
                ),
                "kind": "firmware",
                "family": "mt76",
            },
        ]
        return {
            "schema_version": 1,
            "source": source,
            "source_member_count": 4,
            "retained_member_count": 3,
            "family_counts": {"attribution": 2, "mt76": 1},
            "family_provenance": {
                "mt76": {
                    "license": WIRELESS_FIRMWARE_LICENSE,
                    "license_evidence": [
                        {
                            "uri": (
                                "https://example.com/licenses/"
                                f"{COMMIT}/mediatek.txt"
                            ),
                            "sha256": _digest(self.license_payload),
                            "source_commit": COMMIT,
                            "size": len(self.license_payload),
                            "packaged_path": "LICENSES/LICENCE.fixture",
                            "repository_path": (
                                "packaging/wireless-firmware/licenses/"
                                "LICENCE.fixture"
                            ),
                            "repository_encoding": "identity",
                        }
                    ],
                    "provenance_status": WIRELESS_FIRMWARE_PROVENANCE_STATUS,
                    "reproducible_source": False,
                    "source": {
                        "repository": REPOSITORY,
                        "commit": COMMIT,
                    },
                }
            },
            "classifications": {
                "attribution": {
                    "provenance_status": (
                        "audited-upstream-text-at-pinned-source-commit"
                    ),
                    "reproducible_source": True,
                },
                "firmware": {
                    "license": WIRELESS_FIRMWARE_LICENSE,
                    "provenance_status": WIRELESS_FIRMWARE_PROVENANCE_STATUS,
                    "reproducible_source": False,
                },
            },
            "generated_aliases": [],
            "known_excluded_elf_members": [
                {
                    "path": "system/xbin/excluded-elf",
                    "size": len(self.payloads["system/xbin/excluded-elf"]),
                    "sha256": _digest(self.payloads["system/xbin/excluded-elf"]),
                    "reason": "fixture executable is excluded",
                }
            ],
            "known_gaps": [
                {
                    "id": gap_id,
                    "classification": "fixture-gap",
                    "reason": f"Fixture records {gap_id}.",
                }
                for gap_id in sorted(WIRELESS_FIRMWARE_REQUIRED_GAP_IDS)
            ],
            "members": members,
        }

    def _write_policy(self) -> None:
        path = (
            self.root
            / "packaging"
            / "wireless-firmware"
            / "SOURCE-MEMBER-POLICY.json"
        )
        path.write_text(
            json.dumps(self.policy, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    def _curate(self, name: str = "curated") -> tuple[Path, dict[str, object]]:
        tree = self.root / name
        result = _curate_wireless_firmware_tree(
            root=self.root,
            source=self.source,
            destination=tree,
            dependency=self.dependency,
            rtw88_dependency=self.rtw88_dependency,
        )
        return tree, result

    def test_curates_exact_members_and_embeds_expanded_provenance(self) -> None:
        tree, result = self._curate()
        archive_path = self.root / "curated.zip"
        deterministic_zip(tree, archive_path, epoch=1783987200)
        _verify_curated_wireless_firmware_zip(
            archive_path,
            result["output_records"],
        )
        with zipfile.ZipFile(archive_path, "r") as archive:
            names = {
                info.filename for info in archive.infolist() if not info.is_dir()
            }
            provenance = json.loads(
                archive.read(WIRELESS_FIRMWARE_PROVENANCE).decode("utf-8")
            )
        self.assertEqual(
            names,
            {
                "CURATION-README.md",
                "UPSTREAM-PACKAGE-LICENSE.md",
                "UPSTREAM-README.md",
                "WHENCE",
                "LICENSES/LICENCE.fixture",
                WIRELESS_FIRMWARE_PROVENANCE,
                "system/etc/firmware/mediatek/fixture.bin",
            },
        )
        self.assertNotIn("system/xbin/excluded-elf", names)
        self.assertEqual(provenance["source_member_count"], 4)
        self.assertEqual(provenance["retained_upstream_member_count"], 3)
        self.assertEqual(provenance["excluded_source_member_count"], 1)
        firmware = next(
            record
            for record in provenance["members"]
            if record["kind"] == "firmware"
        )
        self.assertEqual(firmware["license"], WIRELESS_FIRMWARE_LICENSE)
        self.assertFalse(firmware["reproducible_source"])
        self.assertEqual(firmware["source"]["release_commit"], COMMIT)
        self.assertEqual(
            firmware["authoritative_source"]["path"],
            "mediatek/fixture.bin",
        )
        self.assertEqual(
            provenance["source"]["license_classification"],
            WIRELESS_FIRMWARE_LOCK_LICENSE,
        )
        self.assertEqual(
            {gap["id"] for gap in provenance["known_gaps"]},
            set(WIRELESS_FIRMWARE_REQUIRED_GAP_IDS),
        )

    def test_policy_rejects_dependency_commit_drift(self) -> None:
        changed = deepcopy(self.policy)
        changed["source"]["release_commit"] = "2" * 40
        self.policy = changed
        self._write_policy()
        with self.assertRaisesRegex(BuildToolError, "source differs"):
            self._curate()

    def test_member_digest_mismatch_is_rejected(self) -> None:
        changed = deepcopy(self.policy)
        changed["members"][2]["sha256"] = "f" * 64
        self.policy = changed
        self._write_policy()
        with self.assertRaisesRegex(BuildToolError, "differs from policy"):
            self._curate()

    def test_selected_elf_is_rejected(self) -> None:
        member = "system/etc/firmware/mediatek/fixture.bin"
        self.payloads[member] = b"\x7fELFselected\n"
        self._write_source()
        self.dependency = self._dependency()
        changed = self._policy()
        self.policy = changed
        self._write_policy()
        with self.assertRaisesRegex(BuildToolError, "selects forbidden ELF"):
            self._curate()

    def test_undeclared_source_elf_is_rejected(self) -> None:
        changed = deepcopy(self.policy)
        changed["known_excluded_elf_members"] = []
        self.policy = changed
        self._write_policy()
        with self.assertRaisesRegex(BuildToolError, "ELF inventory differs"):
            self._curate()

    def test_lock_license_classification_is_required(self) -> None:
        raw = dict(self.dependency.raw)
        raw["license"] = "SEE-UPSTREAM"
        self.dependency = Dependency(
            id=self.dependency.id,
            kind=self.dependency.kind,
            url=self.dependency.url,
            commit=self.dependency.commit,
            ref=self.dependency.ref,
            sha256=self.dependency.sha256,
            required_for=self.dependency.required_for,
            raw=raw,
        )
        with self.assertRaisesRegex(BuildToolError, "immutable release/source identity"):
            self._curate()

    def test_packaged_license_bytes_are_policy_bound(self) -> None:
        path = (
            self.root
            / "packaging"
            / "wireless-firmware"
            / "licenses"
            / "LICENCE.fixture"
        )
        path.write_bytes(b"changed fixture license\n")
        with self.assertRaisesRegex(BuildToolError, "license differs from policy"):
            self._curate()

    def test_required_broadcom_and_intel_gaps_are_policy_driven(self) -> None:
        changed = deepcopy(self.policy)
        changed["known_gaps"] = [
            gap for gap in changed["known_gaps"] if gap["id"] != "broadcom-hcd"
        ]
        self.policy = changed
        self._write_policy()
        with self.assertRaisesRegex(BuildToolError, "omits broadcom-hcd"):
            self._curate()

    def test_authoritative_repository_paths_are_exact_and_safe(self) -> None:
        self.assertEqual(
            _wireless_authoritative_repository_path(
                "system/etc/firmware/rtl_bt/rtl8852au_fw.bin",
                family="usb-bluetooth-realtek",
            ),
            "rtl_bt/rtl8852au_fw.bin",
        )
        self.assertEqual(
            _wireless_authoritative_repository_path(
                "system/etc/firmware/mt7601u.bin",
                family="mt76",
            ),
            "mediatek/mt7601u.bin",
        )
        self.assertEqual(
            _wireless_authoritative_repository_path(
                "system/etc/firmware/rtw88/rtw8822c_fw.bin",
                family="rtw88",
            ),
            "firmware/rtw8822c_fw.bin",
        )

    def test_rtw88_policy_source_is_bound_to_dependency_lock(self) -> None:
        changed = deepcopy(self.policy)
        changed["members"][2]["family"] = "rtw88"
        changed["family_counts"] = {"attribution": 2, "rtw88": 1}
        family = changed["family_provenance"].pop("mt76")
        family["source"] = {
            "repository": self.rtw88_dependency.url,
            "commit": self.rtw88_dependency.commit,
        }
        changed["family_provenance"]["rtw88"] = family
        self.policy = changed
        self._write_policy()
        drifted = Dependency(
            id=self.rtw88_dependency.id,
            kind=self.rtw88_dependency.kind,
            url=self.rtw88_dependency.url,
            commit="4" * 40,
            ref="4" * 40,
            sha256=None,
            required_for=self.rtw88_dependency.required_for,
            raw=self.rtw88_dependency.raw,
        )
        with self.assertRaisesRegex(BuildToolError, "RTW88 source differs"):
            _load_wireless_firmware_policy(
                root=self.root,
                dependency=self.dependency,
                rtw88_dependency=drifted,
            )

    def test_final_member_namespace_collision_is_rejected(self) -> None:
        with patch(
            "lib.artifacts.WIRELESS_FIRMWARE_README",
            "UPSTREAM-README.md",
        ):
            with self.assertRaisesRegex(BuildToolError, "final member.*collides"):
                _load_wireless_firmware_policy(
                    root=self.root,
                    dependency=self.dependency,
                    rtw88_dependency=self.rtw88_dependency,
                )

    def test_generated_alias_must_source_mt76(self) -> None:
        changed = deepcopy(self.policy)
        changed["members"][2]["family"] = "ath10k"
        changed["family_counts"] = {"ath10k": 1, "attribution": 2}
        changed["family_provenance"]["ath10k"] = changed[
            "family_provenance"
        ].pop("mt76")
        source = changed["members"][2]
        changed["generated_aliases"] = [
            {
                "path": "system/etc/firmware/fixture-alias.bin",
                "source_path": source["path"],
                "size": source["size"],
                "sha256": source["sha256"],
                "reason": "fixture alias",
            }
        ]
        self.policy = changed
        self._write_policy()
        with self.assertRaisesRegex(BuildToolError, "generated-alias.*invalid"):
            _load_wireless_firmware_policy(
                root=self.root,
                dependency=self.dependency,
                rtw88_dependency=self.rtw88_dependency,
            )

    def test_repository_extensionless_license_metadata_is_lf_bound(self) -> None:
        attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
        self.assertIn("packaging/anykernel3/licenses/* text eol=lf", attributes)
        self.assertIn("packaging/wireless-firmware/WHENCE text eol=lf", attributes)
        self.assertIn("packaging/wireless-firmware/licenses/* text eol=lf", attributes)
        extensionless = [
            ROOT / "packaging" / "anykernel3" / "licenses" / "GPL-2.0-only",
            ROOT / "packaging" / "anykernel3" / "licenses" / "GPL-3.0-or-later",
            ROOT / "packaging" / "wireless-firmware" / "WHENCE",
            ROOT / "packaging" / "wireless-firmware" / "licenses" / "LICENCE.mediatek",
        ]
        for path in extensionless:
            self.assertNotIn(b"\r", path.read_bytes(), path.as_posix())

    def test_completed_zip_rejects_undeclared_member(self) -> None:
        tree, result = self._curate()
        archive_path = self.root / "curated.zip"
        deterministic_zip(tree, archive_path, epoch=1783987200)
        with zipfile.ZipFile(
            archive_path,
            "a",
            compression=zipfile.ZIP_DEFLATED,
        ) as archive:
            archive.writestr("undeclared.bin", b"undeclared\n")
        with self.assertRaisesRegex(BuildToolError, "ZIP contents differ"):
            _verify_curated_wireless_firmware_zip(
                archive_path,
                result["output_records"],
            )

    def test_real_pinned_asset_is_deterministically_curated(self) -> None:
        source = (
            ROOT
            / ".cache"
            / "op13"
            / "files"
            / "nethunter_wireless_firmware-872dc926dce8.zip"
        )
        if not source.is_file():
            self.skipTest("real pinned wireless firmware asset is not cached")
        lock = load_dependency_lock(ROOT / "dependencies" / "lock.yml")
        dependency = lock.dependencies["nethunter_wireless_firmware"]
        rtw88_dependency = lock.dependencies["rtw88"]
        first_tree = self.root / "real-first"
        second_tree = self.root / "real-second"
        first = _curate_wireless_firmware_tree(
            root=ROOT,
            source=source,
            destination=first_tree,
            dependency=dependency,
            rtw88_dependency=rtw88_dependency,
        )
        second = _curate_wireless_firmware_tree(
            root=ROOT,
            source=source,
            destination=second_tree,
            dependency=dependency,
            rtw88_dependency=rtw88_dependency,
        )
        first_zip = self.root / "real-first.zip"
        second_zip = self.root / "real-second.zip"
        deterministic_zip(first_tree, first_zip, epoch=1783987200)
        deterministic_zip(second_tree, second_zip, epoch=1783987200)
        _verify_curated_wireless_firmware_zip(
            first_zip,
            first["output_records"],
        )
        self.assertEqual(sha256_file(first_zip), sha256_file(second_zip))
        self.assertEqual(first["source_member_count"], 274)
        self.assertEqual(first["retained_upstream_member_count"], 65)
        self.assertEqual(first["excluded_source_member_count"], 209)
        self.assertEqual(first["firmware_member_count"], 63)
        self.assertEqual(first["generated_alias_count"], 2)
        self.assertEqual(first["license_text_member_count"], 7)
        self.assertEqual(first["curated_attribution_member_count"], 9)
        self.assertEqual(first["known_gap_count"], 7)
        self.assertEqual(
            first["lock_license_classification"],
            WIRELESS_FIRMWARE_LOCK_LICENSE,
        )
        self.assertEqual(first["elf_member_count"], 0)
        with zipfile.ZipFile(first_zip, "r") as archive:
            names = {
                info.filename for info in archive.infolist() if not info.is_dir()
            }
            provenance = json.loads(
                archive.read(WIRELESS_FIRMWARE_PROVENANCE).decode("utf-8")
            )
            self.assertEqual(len(names), 77)
            self.assertIn("system/etc/firmware/mt7662.bin", names)
            self.assertIn("system/etc/firmware/mt7662_rom_patch.bin", names)
            self.assertIn("WHENCE", names)
            self.assertIn("UPSTREAM-README.md", names)
            self.assertIn("UPSTREAM-PACKAGE-LICENSE.md", names)
            self.assertNotIn("README.md", names)
            self.assertNotIn("LICENSE.md", names)
            self.assertIn(
                "LICENSES/LICENCE.open-ath9k-htc-firmware",
                names,
            )
            self.assertIn("LICENSES/notice_ath10k_firmware-5.txt", names)
            self.assertIn("LICENSES/notice_ath10k_firmware-6.txt", names)
            self.assertEqual(
                _digest(archive.read("LICENSES/notice_ath10k_firmware-5.txt")),
                "7fef27f33c95ed680c21809edacdd90736ed3c903e6c224eb72f947c35e9856c",
            )
            self.assertEqual(
                _digest(archive.read("LICENSES/notice_ath10k_firmware-6.txt")),
                "8ce5c6ea0542bf4aac31fc3ae16a39792ad22d0eae4543063fac56fb3380f021",
            )
            self.assertEqual(
                _digest(
                    archive.read(
                        "LICENSES/LICENCE.open-ath9k-htc-firmware"
                    )
                ),
                "83870e84c54aa76be973a78387b342c80ef0908b8cb82edde6d54c511b18c16c",
            )
            self.assertNotIn("system/xbin/hid-keyboard", names)
            self.assertFalse(any("/scp.img" in name for name in names))
            self.assertFalse(any("/sof/" in name for name in names))
            self.assertFalse(any("/vpu_" in name for name in names))
            self.assertFalse(
                any(archive.read(name)[:4] == b"\x7fELF" for name in names)
            )
            self.assertTrue(
                {"broadcom-hcd", "intel-bluetooth"}
                <= {gap["id"] for gap in provenance["known_gaps"]}
            )
            self.assertEqual(
                provenance["source"]["license_classification"],
                WIRELESS_FIRMWARE_LOCK_LICENSE,
            )
            members = {
                record["path"]: record for record in provenance["members"]
            }
            ath9k = members[
                "system/etc/firmware/ath9k_htc/htc_9271-1.4.0.fw"
            ]
            self.assertEqual(ath9k["license"], "FREE-SOFTWARE-SOURCE-AVAILABLE")
            self.assertEqual(
                ath9k["authoritative_source"]["commit"],
                "195e420f34c5c029d27ee57c57e3b3a8a164f8c4",
            )
            self.assertEqual(
                ath9k["authoritative_source"]["path"],
                "ath9k_htc/htc_9271-1.4.0.fw",
            )
            rtl8852au = members[
                "system/etc/firmware/rtl_bt/rtl8852au_fw.bin"
            ]
            self.assertEqual(
                rtl8852au["authoritative_source"]["commit"],
                "1cd1c871c9162a2c34380e6dd0c8ac9474d36522",
            )
            rtw88 = members["system/etc/firmware/rtw88/rtw8822c_fw.bin"]
            self.assertEqual(
                rtw88["authoritative_source"]["commit"],
                "a56bcd26e770257612a0803249cbd4095fc6feca",
            )
            self.assertEqual(
                rtw88["authoritative_source"]["path"],
                "firmware/rtw8822c_fw.bin",
            )
            mt7601 = members["system/etc/firmware/mt7601u.bin"]
            self.assertEqual(
                mt7601["authoritative_source"]["path"],
                "mediatek/mt7601u.bin",
            )
            for record in members.values():
                if record["kind"] != "firmware":
                    continue
                repository_path = record["authoritative_source"]["path"]
                self.assertFalse(repository_path.startswith("/"))
                self.assertNotIn("..", Path(repository_path).parts)
            canonical_mt7662 = archive.read(
                "system/etc/firmware/mediatek/mt7662.bin"
            )
            self.assertEqual(
                archive.read("system/etc/firmware/mt7662.bin"),
                canonical_mt7662,
            )


if __name__ == "__main__":
    unittest.main()
