from __future__ import annotations

import gzip
import hashlib
import io
import json
import stat
import sys
import tarfile
import tempfile
import unittest
import zipfile
from copy import deepcopy
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

from lib.build_evidence import (
    KMI_NAME,
    TOOLCHAIN_NAME,
    WIRELESS_LED_KMI_NAME,
    copy_preserved_build_evidence,
)
from lib import artifacts as artifact_lib
from lib import release_provenance as release_lib
from lib.release_provenance import (
    ANYKERNEL_RELEASE_MODES,
    ReleaseProvenanceError,
    _inventory_from_lock,
    _validate_corresponding_source_companion,
    generate_release_provenance,
)
from test_build_evidence import BuildEvidenceFixture


REPOSITORY = "Hipuu/OnePlus13-KernelBuilder"
REVISION = "1" * 40
MANIFEST_REVISION = "2" * 40


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_sha256(value: object) -> str:
    payload = (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _source_tar_bytes(
    source_root: str,
    *,
    files: dict[str, bytes] | None = None,
) -> bytes:
    payloads = {"README.md": b"release source fixture\n", **(files or {})}
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0) as compressed:
        with tarfile.open(
            fileobj=compressed,
            mode="w",
            format=tarfile.USTAR_FORMAT,
        ) as archive:
            directory = tarfile.TarInfo(source_root)
            directory.type = tarfile.DIRTYPE
            directory.mode = 0o755
            directory.mtime = 0
            archive.addfile(directory)
            for relative, content in sorted(payloads.items()):
                source = tarfile.TarInfo(f"{source_root}/{relative}")
                source.size = len(content)
                source.mode = 0o644
                source.mtime = 0
                archive.addfile(source, io.BytesIO(content))
    return output.getvalue()


def _source_root(record: dict[str, object]) -> str:
    if record["relationship"] == "magisk-cargo-registry":
        package = record["cargo_packages"][0]
        return f"{package['name']}-{package['version']}"
    repository_name = str(record["repository"]).rsplit("/", 1)[1]
    if not repository_name.endswith(".git"):
        raise AssertionError("fixture source repository must end in .git")
    return f"{repository_name[:-4]}-{record['commit']}"


def _cargo_manifest(name: str, version: str, license_value: str) -> bytes:
    return (
        "[package]\n"
        f"name = {json.dumps(name)}\n"
        f"version = {json.dumps(version)}\n"
        f"license = {json.dumps(license_value)}\n"
    ).encode("utf-8")


def _synthetic_cargo_lock(policy: dict[str, object]) -> bytes:
    lines = ["version = 4", ""]
    for index in range(13):
        lines.extend(
            [
                "[[package]]",
                f'name = "local-fixture-{index}"',
                'version = "0.0.0"',
                "",
            ]
        )
    for record in policy["archives"]:
        for package in record["cargo_packages"]:
            lines.extend(
                [
                    "[[package]]",
                    f"name = {json.dumps(package['name'])}",
                    f"version = {json.dumps(package['version'])}",
                    f"source = {json.dumps(package['source'])}",
                ]
            )
            if package["checksum"] is not None:
                lines.append(f"checksum = {json.dumps(package['checksum'])}")
            lines.append("")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _synthetic_gitmodules(policy: dict[str, object]) -> bytes:
    lines: list[str] = []
    for index, record in enumerate(policy["magisk_gitlinks"]):
        lines.extend(
            [
                f'[submodule "fixture-{index}"]',
                f"\tpath = {record['path']}",
                f"\turl = {record['repository']}",
            ]
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def _write_release_zip(
    path: Path,
    members: dict[str, bytes],
    *,
    modes: dict[str, int] | None = None,
    compression: int = zipfile.ZIP_DEFLATED,
) -> None:
    with zipfile.ZipFile(
        path,
        "w",
        compression=compression,
        **({"compresslevel": 9} if compression == zipfile.ZIP_DEFLATED else {}),
    ) as archive:
        for name, payload in sorted(members.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 7, 14, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | (modes or {}).get(name, 0o644)) << 16
            info.compress_type = compression
            archive.writestr(info, payload, compress_type=compression)


def _aarch64_elf(marker: bytes) -> bytes:
    payload = bytearray(64)
    payload[:4] = b"\x7fELF"
    payload[4] = 2
    payload[5] = 1
    payload[6] = 1
    payload[16:18] = (2).to_bytes(2, "little")
    payload[18:20] = (183).to_bytes(2, "little")
    payload[20:24] = (1).to_bytes(4, "little")
    return bytes(payload) + marker


class ReleaseProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tree_verifier_patcher = mock.patch.object(
            artifact_lib,
            "_verify_corresponding_source_git_tree_archive",
            return_value=None,
        )
        self.tree_verifier_patcher.start()
        self.addCleanup(self.tree_verifier_patcher.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.assets = Path(self.temporary.name) / "release-assets"
        self.assets.mkdir()
        self.repository_root = Path(self.temporary.name) / "repository"
        self.resolved_manifest = self.assets / "OnePlus13-oos16-manifest.xml"
        self.resolved_manifest.write_text(
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            "<manifest>\n"
            "  <remote name=\"origin\" fetch=\"https://github.com/OnePlusOSS\"/>\n"
            "  <default remote=\"origin\"/>\n"
            f"  <project name=\"android_kernel_common_oneplus_sm8750\" path=\"kernel_platform/common\" revision=\"{'3' * 40}\"/>\n"
            f"  <project name=\"android_kernel_modules_and_devicetree_oneplus_sm8750\" path=\"./\" revision=\"{'4' * 40}\"/>\n"
            "</manifest>\n",
            encoding="utf-8",
            newline="\n",
        )
        (self.assets / "kernel-Image").write_bytes(b"kernel\n")
        (self.assets / "SHA256SUMS").write_text("fixture\n", encoding="utf-8")
        evidence_root = Path(self.temporary.name) / "evidence-fixture"
        evidence_root.mkdir()
        evidence_fixture = BuildEvidenceFixture(evidence_root)
        evidence_fixture.manifest.write_bytes(self.resolved_manifest.read_bytes())
        evidence_fixture.manifest_sha256 = _sha256(evidence_fixture.manifest)
        toolchain_path = evidence_fixture.source / ".op13" / TOOLCHAIN_NAME
        toolchain = json.loads(toolchain_path.read_text(encoding="utf-8"))
        toolchain["resolved_manifest"].update(
            {
                "size": evidence_fixture.manifest.stat().st_size,
                "sha256": _sha256(evidence_fixture.manifest),
            }
        )
        toolchain_path.write_text(
            json.dumps(toolchain, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        evidence_fixture.install_wireless_led_stamp()
        preserved_evidence = evidence_fixture.preserve(
            wireless_led_exports_required=True
        )
        self.packaged_evidence = copy_preserved_build_evidence(
            input_dir=evidence_fixture.output,
            destination=self.assets,
            evidence_value=preserved_evidence,
            base="oos16",
            resolved_manifest_sha256=_sha256(self.resolved_manifest),
            wireless_led_exports_required=True,
        )
        locked_manifest = self.repository_root / "manifests" / "lockfiles" / "oos16.xml"
        locked_manifest.parent.mkdir(parents=True)
        locked_manifest.write_bytes(self.resolved_manifest.read_bytes())
        self.locked_manifest_sha256 = _sha256(locked_manifest)
        lock_document = {
            "schema_version": 1,
            "dependencies": {
                "anykernel3": {
                    "kind": "git",
                    "url": "https://github.com/osm0sis/AnyKernel3.git",
                    "commit": "5" * 40,
                    "required_for": ["package-anykernel3"],
                },
                "nethunter_wireless_firmware": {
                    "kind": "release_asset",
                    "url": "https://github.com/example/firmware/releases/download/v1/fw.zip",
                    "repository": "https://github.com/example/firmware.git",
                    "commit": "6" * 40,
                    "sha256": "c" * 64,
                    "required_for": ["nethunter", "release"],
                },
                "oneplus_manifest": {
                    "kind": "git",
                    "url": "https://github.com/OnePlusOSS/kernel_manifest.git",
                    "commit": MANIFEST_REVISION,
                    "required_for": ["source-sync"],
                },
                "repo_launcher": {
                    "kind": "file",
                    "url": "https://storage.googleapis.com/git-repo-downloads/repo-2.54",
                    "sha256": "d" * 64,
                    "repo_url": "https://gerrit.googlesource.com/git-repo",
                    "repo_commit": "7" * 40,
                    "required_for": ["source-sync"],
                },
            },
        }
        checked_lock = json.loads(
            (ROOT / "dependencies" / "lock.yml").read_text(encoding="utf-8")
        )
        lock_document["dependencies"]["anykernel3"] = deepcopy(
            checked_lock["dependencies"]["anykernel3"]
        )
        self.source_policy = json.loads(
            (
                ROOT
                / "packaging"
                / "anykernel3"
                / "CORRESPONDING-SOURCE.json"
            ).read_text(encoding="utf-8")
        )
        self.source_payloads: dict[str, bytes] = {}
        lock_document["dependencies"]["magisk_release_apk"] = deepcopy(
            checked_lock["dependencies"]["magisk_release_apk"]
        )
        for index, policy_record in enumerate(self.source_policy["archives"]):
            if policy_record["dependency"] == "magisk_source":
                continue
            dependency_id = policy_record["dependency"]
            files = {
                package["manifest_path"]: _cargo_manifest(
                    package["name"],
                    package["version"],
                    policy_record["license"],
                )
                for package in policy_record["cargo_packages"]
            }
            payload = _source_tar_bytes(
                _source_root(policy_record),
                files=files,
            )
            self.source_payloads[policy_record["archive_path"]] = payload
            policy_record["size"] = len(payload)
            policy_record["sha256"] = hashlib.sha256(payload).hexdigest()
            if policy_record["relationship"] == "magisk-cargo-registry":
                policy_record["cargo_packages"][0]["checksum"] = policy_record[
                    "sha256"
                ]
            dependency = deepcopy(checked_lock["dependencies"][dependency_id])
            dependency["size"] = len(payload)
            dependency["sha256"] = policy_record["sha256"]
            lock_document["dependencies"][dependency_id] = dependency

        cargo_lock = _synthetic_cargo_lock(self.source_policy)
        gitmodules = _synthetic_gitmodules(self.source_policy)
        magisk_record = self.source_policy["archives"][0]
        magisk_root = _source_root(magisk_record)
        self.source_policy["cargo_lock"].update(
            {
                "archive_member": f"{magisk_root}/native/src/Cargo.lock",
                "size": len(cargo_lock),
                "sha256": hashlib.sha256(cargo_lock).hexdigest(),
            }
        )
        self.source_policy["magisk_gitmodules"].update(
            {
                "archive_member": f"{magisk_root}/.gitmodules",
                "size": len(gitmodules),
                "sha256": hashlib.sha256(gitmodules).hexdigest(),
            }
        )
        magisk_payload = _source_tar_bytes(
            magisk_root,
            files={
                ".gitmodules": gitmodules,
                "native/src/Cargo.lock": cargo_lock,
            },
        )
        self.source_payloads[magisk_record["archive_path"]] = magisk_payload
        magisk_record["size"] = len(magisk_payload)
        magisk_record["sha256"] = hashlib.sha256(magisk_payload).hexdigest()
        magisk_lock = deepcopy(checked_lock["dependencies"]["magisk_source"])
        magisk_lock["size"] = len(magisk_payload)
        magisk_lock["sha256"] = magisk_record["sha256"]
        lock_document["dependencies"]["magisk_source"] = magisk_lock

        policy_path = (
            self.repository_root
            / "packaging"
            / "anykernel3"
            / "CORRESPONDING-SOURCE.json"
        )
        policy_path.parent.mkdir(parents=True)
        self.source_policy_bytes = (
            json.dumps(self.source_policy, indent=2) + "\n"
        ).encode("utf-8")
        policy_path.write_bytes(self.source_policy_bytes)
        executable_policy = (
            ROOT
            / "packaging"
            / "anykernel3"
            / "EXECUTABLE-PROVENANCE.json"
        )
        executable_document = json.loads(executable_policy.read_text(encoding="utf-8"))
        self.anykernel_tools = {
            "tools/busybox": _aarch64_elf(b"busybox-fixture"),
            "tools/magiskboot": _aarch64_elf(b"magiskboot-fixture"),
        }
        self.anykernel_template_payloads = {
            "LICENSE": b"AnyKernel fixture license\n",
            "META-INF/com/google/android/update-binary": b"#!/sbin/sh\n",
            "META-INF/com/google/android/updater-script": b"#MAGISK\n",
            "tools/ak3-core.sh": b"#!/sbin/sh\n",
        }
        self.anykernel_upstream_contracts = tuple(
            {
                "path": member,
                "git_mode": (
                    "100755" if ANYKERNEL_RELEASE_MODES[member] == 0o755 else "100644"
                ),
                "git_blob": hashlib.sha1(
                    b"blob "
                    + str(len(payload)).encode("ascii")
                    + b"\0"
                    + payload,
                    usedforsecurity=False,
                ).hexdigest(),
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
            for member, payload in self.anykernel_template_payloads.items()
        )
        executable_document["anykernel3"]["template_members"] = [
            dict(record) for record in self.anykernel_upstream_contracts
        ]
        self.upstream_contract_patcher = mock.patch.object(
            release_lib,
            "ANYKERNEL_UPSTREAM_MEMBER_CONTRACTS",
            self.anykernel_upstream_contracts,
        )
        self.upstream_contract_patcher.start()
        self.addCleanup(self.upstream_contract_patcher.stop)
        for record in executable_document["executables"]:
            payload = self.anykernel_tools[record["path"]]
            record["size"] = len(payload)
            record["sha256"] = hashlib.sha256(payload).hexdigest()
        self.executable_policy_bytes = (
            json.dumps(executable_document, indent=2) + "\n"
        ).encode("utf-8")
        (policy_path.parent / "EXECUTABLE-PROVENANCE.json").write_bytes(
            self.executable_policy_bytes
        )
        for relative in (
            "anykernel.sh",
            "SOURCE-CONVEYANCE.md",
            "licenses/GPL-2.0-only",
            "licenses/GPL-3.0-or-later",
        ):
            source = ROOT / "packaging" / "anykernel3" / relative
            destination = policy_path.parent / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read_bytes())
        lock_path = self.repository_root / "dependencies" / "lock.yml"
        lock_path.parent.mkdir(parents=True)
        lock_path.write_text(
            json.dumps(lock_document, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        self.lock_sha256 = _sha256(lock_path)
        self.lock_canonical_sha256 = _canonical_sha256(lock_document)
        self.lock_document = lock_document
        executable_digests = {
            record["path"]: record["sha256"]
            for record in executable_document["executables"]
        }
        magisk_dependency = lock_document["dependencies"]["magisk_release_apk"]
        source_manifest = {
            "schema_version": 1,
            "format": "oneplus13-anykernel-corresponding-source",
            "scope": self.source_policy["scope"],
            "dependency_lock_sha256": self.lock_canonical_sha256,
            "source_policy": {
                "repository_path": "packaging/anykernel3/CORRESPONDING-SOURCE.json",
                "member": "SOURCE-POLICY.json",
                "sha256": hashlib.sha256(self.source_policy_bytes).hexdigest(),
            },
            "release_asset": {
                "dependency": "magisk_release_apk",
                "uri": magisk_dependency["url"],
                "sha256": magisk_dependency["sha256"],
                "repository": magisk_dependency["repository"],
                "commit": magisk_dependency["commit"],
                "version": magisk_dependency["version"],
            },
            "binary_relationships": [
                {
                    "path": "tools/busybox",
                    "sha256": executable_digests["tools/busybox"],
                    "source_dependencies": ["magisk_busybox_source"],
                },
                {
                    "path": "tools/magiskboot",
                    "sha256": executable_digests["tools/magiskboot"],
                    "source_dependencies": [
                        "magisk_source",
                        *sorted(
                            record["dependency"]
                            for record in self.source_policy["archives"][2:]
                        ),
                    ],
                },
            ],
            "archives": self.source_policy["archives"],
        }
        self.source_manifest_bytes = (
            json.dumps(source_manifest, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        self.companion = (
            self.assets
            / "OnePlus13-oos16-full-kernelsu-next-corresponding-source.zip"
        )
        _write_release_zip(
            self.companion,
            {
                **self.source_payloads,
                "SOURCE-MANIFEST.json": self.source_manifest_bytes,
                "SOURCE-POLICY.json": self.source_policy_bytes,
            },
            compression=zipfile.ZIP_STORED,
        )
        self.anykernel = self.assets / "kernel-AnyKernel3.zip"
        self.module_zip = self.assets / "kernel-modules.zip"
        self.firmware_zip = self.assets / "kernel-wireless-firmware.zip"
        self.debug_zip = self.assets / "kernel-debug.zip"
        anykernel_overlay = self.repository_root / "packaging" / "anykernel3"
        _write_release_zip(
            self.anykernel,
            {
                "EXECUTABLE-PROVENANCE.json": self.executable_policy_bytes,
                "Image": (self.assets / "kernel-Image").read_bytes(),
                "LICENSE": self.anykernel_template_payloads["LICENSE"],
                "LICENSES/GPL-2.0-only": (
                    anykernel_overlay / "licenses" / "GPL-2.0-only"
                ).read_bytes(),
                "LICENSES/GPL-3.0-or-later": (
                    anykernel_overlay / "licenses" / "GPL-3.0-or-later"
                ).read_bytes(),
                "META-INF/com/google/android/update-binary": (
                    self.anykernel_template_payloads[
                        "META-INF/com/google/android/update-binary"
                    ]
                ),
                "META-INF/com/google/android/updater-script": (
                    self.anykernel_template_payloads[
                        "META-INF/com/google/android/updater-script"
                    ]
                ),
                "SOURCE-CONVEYANCE.md": (
                    anykernel_overlay / "SOURCE-CONVEYANCE.md"
                ).read_bytes(),
                "anykernel.sh": (anykernel_overlay / "anykernel.sh").read_bytes(),
                "tools/ak3-core.sh": self.anykernel_template_payloads[
                    "tools/ak3-core.sh"
                ],
                **self.anykernel_tools,
            },
            modes=dict(ANYKERNEL_RELEASE_MODES),
        )
        for path, payload in (
            (self.module_zip, b"module fixture\n"),
            (self.firmware_zip, b"firmware fixture\n"),
            (self.debug_zip, b"debug fixture\n"),
        ):
            path.write_bytes(payload)

        def artifact(path: Path, role: str, **metadata: object) -> dict[str, object]:
            return {
                "path": f"out/dist/{path.name}",
                "role": role,
                "size": path.stat().st_size,
                "sha256": _sha256(path),
                **metadata,
            }

        self.artifacts = [
            artifact(self.assets / TOOLCHAIN_NAME, "build-toolchain-provenance"),
            artifact(self.assets / KMI_NAME, "kmi-symbol-exports"),
            artifact(
                self.assets / WIRELESS_LED_KMI_NAME,
                "wireless-led-kmi-symbol-exports",
            ),
            artifact(self.assets / "kernel-Image", "kernel-image"),
            artifact(
                self.anykernel,
                "anykernel3-zip",
                dependencies=["anykernel3", "magisk_release_apk"],
                member_count=len(ANYKERNEL_RELEASE_MODES),
                member_mode_policy="explicit-host-independent",
                elf_class="ELFCLASS64",
                elf_machine="EM_AARCH64",
                executable_provenance_member="EXECUTABLE-PROVENANCE.json",
                executable_provenance_sha256=hashlib.sha256(
                    self.executable_policy_bytes
                ).hexdigest(),
                magisk_release_sha256=lock_document["dependencies"]
                ["magisk_release_apk"]["sha256"],
            ),
            artifact(
                self.companion,
                "corresponding-source",
                dependencies=[
                    record["dependency"]
                    for record in self.source_policy["archives"]
                ],
                archive_count=len(self.source_policy["archives"]),
                member_count=len(self.source_policy["archives"]) + 2,
                member_mode_policy="all-regular-0644",
                source_manifest_member="SOURCE-MANIFEST.json",
                source_manifest_sha256=hashlib.sha256(
                    self.source_manifest_bytes
                ).hexdigest(),
                source_policy_member="SOURCE-POLICY.json",
                source_policy_sha256=hashlib.sha256(
                    self.source_policy_bytes
                ).hexdigest(),
                scope=self.source_policy["scope"],
                reproducible_build_proof=False,
            ),
            artifact(self.module_zip, "module-zip"),
            artifact(self.firmware_zip, "wireless-firmware"),
            artifact(self.debug_zip, "debug-zip"),
            artifact(self.resolved_manifest, "resolved-manifest"),
        ]
        self.build_manifest = {
            "schema_version": 2,
            "builder": {
                "repository": REPOSITORY,
                "revision": REVISION,
                "workflow_run_id": "1234",
            },
            "base": "oos16",
            "root_variant": "kernelsu-next",
            "feature_profile": "full",
            "features": [
                {
                    "profile": "full",
                    "root_variant": "kernelsu-next",
                    "flags": {
                        "artifact.wireless_firmware": True,
                        "nethunter.wifi_ath": True,
                    },
                }
            ],
            "build_target": "mixed",
            "debug": True,
            "pre_release": True,
            "smoke": False,
            "source": {
                "url": "https://github.com/OnePlusOSS/kernel_manifest.git",
                "branch": "oneplus/sm8750",
                "file": "oneplus_13_b.xml",
                "revision": MANIFEST_REVISION,
                "locked_path": "manifests/lockfiles/oos16.xml",
                "locked_sha256": self.locked_manifest_sha256,
                "resolved_path": self.resolved_manifest.name,
                "sha256": _sha256(self.resolved_manifest),
            },
            "build_evidence": self.packaged_evidence,
            "dependency_lock": {
                "path": "dependencies/lock.yml",
                "sha256": self.lock_sha256,
                "canonical_sha256": self.lock_canonical_sha256,
            },
            "dependencies": [
                {
                    "id": "anykernel3",
                    "kind": "git",
                    "required_for": ["package-anykernel3"],
                    "source": {
                        "uri": "https://github.com/osm0sis/AnyKernel3.git",
                        "commit": "5" * 40,
                    },
                },
                {
                    "id": "nethunter_wireless_firmware",
                    "kind": "release_asset",
                    "required_for": ["nethunter", "release"],
                    "resource": {
                        "uri": "https://github.com/example/firmware/releases/download/v1/fw.zip",
                        "sha256": "c" * 64,
                    },
                    "source": {
                        "uri": "https://github.com/example/firmware.git",
                        "commit": "6" * 40,
                    },
                },
                {
                    "id": "oneplus_manifest",
                    "kind": "git",
                    "required_for": ["source-sync"],
                    "source": {
                        "uri": "https://github.com/OnePlusOSS/kernel_manifest.git",
                        "commit": MANIFEST_REVISION,
                    },
                },
                {
                    "id": "repo_launcher",
                    "kind": "file",
                    "required_for": ["source-sync"],
                    "resource": {
                        "uri": "https://storage.googleapis.com/git-repo-downloads/repo-2.54",
                        "sha256": "d" * 64,
                    },
                    "source": {
                        "uri": "https://gerrit.googlesource.com/git-repo",
                        "commit": "7" * 40,
                    },
                },
            ],
            "configuration": {"optimization": "O2", "lto": "thin"},
            "kernel": {
                # A modules-only release can package debug evidence while its
                # reused prerequisite kernel was built without debug mode.
                "debug": False,
                "branding": "OnePlus13-KernelBuilder",
                "source_date_epoch": 1783987200,
                "build_timestamp": {
                    "artifact_key": "default",
                    "mode": "default",
                    "requested": None,
                    "requested_sha256": None,
                    "source_date_epoch": 1783987200,
                },
            },
        }
        self.build_manifest["dependencies"] = _inventory_from_lock(lock_document)
        self.build_manifest["artifacts"] = self.artifacts
        self._write_build_manifest()
        self.parameters = {
            "tag": "v1.0.0",
            "base": "oos16",
            "root": "kernelsu-next",
            "profile": "full",
            "target": "mixed",
            "optimization": "O2",
            "lto": "thin",
            "clean": True,
            "debug": True,
            "preRelease": True,
            "branding": "OnePlus13-KernelBuilder",
            "buildTimestamp": "",
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_build_manifest(self) -> None:
        manifest_path = self.assets / "BUILD-MANIFEST.json"
        manifest_path.write_text(
            json.dumps(self.build_manifest, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        names = {
            Path(record["path"]).name for record in self.build_manifest["artifacts"]
        }
        names.add(manifest_path.name)
        (self.assets / "SHA256SUMS").write_text(
            "".join(
                f"{_sha256(self.assets / name)}  {name}\n"
                for name in sorted(names, key=lambda value: value.encode("utf-8"))
            ),
            encoding="ascii",
            newline="\n",
        )

    def _reseal_companion_metadata(
        self,
        *,
        date_time: tuple[int, int, int, int, int, int] = (2026, 7, 14, 0, 0, 0),
        extra: bytes = b"",
        archive_comment: bytes = b"",
    ) -> None:
        with zipfile.ZipFile(self.companion, "r") as archive:
            members = {info.filename: archive.read(info) for info in archive.infolist()}
        with zipfile.ZipFile(
            self.companion,
            "w",
            compression=zipfile.ZIP_STORED,
        ) as archive:
            for index, (name, payload) in enumerate(sorted(members.items())):
                info = zipfile.ZipInfo(name, date_time=date_time)
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                info.compress_type = zipfile.ZIP_STORED
                if index == 0:
                    info.extra = extra
                archive.writestr(info, payload, compress_type=zipfile.ZIP_STORED)
            archive.comment = archive_comment
        record = next(
            item
            for item in self.build_manifest["artifacts"]
            if item["role"] == "corresponding-source"
        )
        record["size"] = self.companion.stat().st_size
        record["sha256"] = _sha256(self.companion)
        self._write_build_manifest()

    def _generate(self) -> tuple[Path, Path]:
        return generate_release_provenance(
            assets_dir=self.assets,
            repository_root=self.repository_root,
            repository=REPOSITORY,
            revision=REVISION,
            run_id="1234",
            run_attempt="2",
            external_parameters=self.parameters,
        )

    def test_records_locked_manifests_projects_and_dependency_digests(self) -> None:
        provenance_path, checksum_path = self._generate()
        first_provenance = provenance_path.read_bytes()
        first_checksums = checksum_path.read_bytes()
        self._generate()
        self.assertEqual(provenance_path.read_bytes(), first_provenance)
        self.assertEqual(checksum_path.read_bytes(), first_checksums)

        statement = json.loads(first_provenance)
        definition = statement["predicate"]["buildDefinition"]
        dependencies = {record["name"]: record for record in definition["resolvedDependencies"]}
        self.assertEqual(dependencies["orchestrator"]["digest"], {"gitCommit": REVISION})
        self.assertEqual(
            dependencies["dependency-lock"]["digest"],
            {"sha256": self.lock_sha256},
        )
        self.assertEqual(
            dependencies["oneplus-manifest-lock:oos16"]["digest"],
            {"sha256": self.locked_manifest_sha256},
        )
        self.assertEqual(
            dependencies["oneplus-manifest-resolved:oos16"]["digest"],
            {"sha256": _sha256(self.resolved_manifest)},
        )
        self.assertEqual(
            dependencies["locked-dependency:anykernel3"]["digest"],
            {
                "gitCommit": self.lock_document["dependencies"]["anykernel3"]
                ["commit"]
            },
        )
        self.assertEqual(
            dependencies["locked-dependency:repo_launcher"]["digest"],
            {"sha256": "d" * 64},
        )
        self.assertEqual(
            dependencies["locked-dependency-source:repo_launcher"]["digest"],
            {"gitCommit": "7" * 40},
        )
        self.assertEqual(
            dependencies["oneplus-project:kernel_platform/common"]["digest"],
            {"gitCommit": "3" * 40},
        )
        self.assertEqual(
            dependencies["oneplus-project:."]["digest"],
            {"gitCommit": "4" * 40},
        )
        self.assertEqual(
            definition["internalParameters"]["lockedDependencyCount"],
            len(self.build_manifest["dependencies"]),
        )
        self.assertEqual(definition["internalParameters"]["onePlusProjectCount"], 2)
        self.assertEqual(
            [record["name"] for record in definition["resolvedDependencies"]],
            sorted(record["name"] for record in definition["resolvedDependencies"]),
        )
        checksum_lines = checksum_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(checksum_lines, sorted(checksum_lines, key=lambda line: line.split("  ", 1)[1]))
        self.assertTrue(any(line.endswith("  provenance.intoto.jsonl") for line in checksum_lines))

    def test_rejects_tampered_resolved_manifest(self) -> None:
        self.resolved_manifest.write_text("<manifest/>\n", encoding="utf-8")
        with self.assertRaisesRegex(ReleaseProvenanceError, "digest differs"):
            self._generate()

    def test_rejects_release_parameter_mismatch(self) -> None:
        self.parameters = {**self.parameters, "root": "kernelsu"}
        with self.assertRaisesRegex(ReleaseProvenanceError, "parameter root differs"):
            self._generate()

    def test_release_timestamp_binds_the_exact_raw_representation(self) -> None:
        raw = "1783987200"
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        self.build_manifest["kernel"]["build_timestamp"] = {
            "artifact_key": digest,
            "mode": "explicit",
            "requested": raw,
            "requested_sha256": digest,
            "source_date_epoch": 1783987200,
        }
        self.parameters = {**self.parameters, "buildTimestamp": raw}
        self._write_build_manifest()
        self._generate()
        self.parameters["buildTimestamp"] = "2026-07-14T00:00:00Z"
        with self.assertRaisesRegex(ReleaseProvenanceError, "raw identity differs"):
            self._generate()

    def test_default_timestamp_is_distinct_from_an_explicit_equal_epoch(self) -> None:
        self.parameters = {**self.parameters, "buildTimestamp": "1783987200"}
        with self.assertRaisesRegex(ReleaseProvenanceError, "raw identity differs"):
            self._generate()

    def test_rejects_tampered_packaged_toolchain_evidence(self) -> None:
        (self.assets / TOOLCHAIN_NAME).write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(ReleaseProvenanceError, "packaged build evidence"):
            self._generate()

    def test_rejects_missing_corresponding_source_companion(self) -> None:
        self.companion.unlink()
        with self.assertRaisesRegex(
            ReleaseProvenanceError,
            "packaged artifact corresponding-source is missing",
        ):
            self._generate()

    def test_rejects_tampered_companion_even_when_outer_hashes_are_resealed(self) -> None:
        with zipfile.ZipFile(self.companion, "r") as archive:
            members = {info.filename: archive.read(info) for info in archive.infolist()}
        source_manifest = json.loads(members["SOURCE-MANIFEST.json"])
        source_manifest["scope"] = "tampered source scope"
        members["SOURCE-MANIFEST.json"] = (
            json.dumps(source_manifest, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        _write_release_zip(
            self.companion,
            members,
            compression=zipfile.ZIP_STORED,
        )
        record = next(
            item
            for item in self.build_manifest["artifacts"]
            if item["role"] == "corresponding-source"
        )
        record["size"] = self.companion.stat().st_size
        record["sha256"] = _sha256(self.companion)
        record["source_manifest_sha256"] = hashlib.sha256(
            members["SOURCE-MANIFEST.json"]
        ).hexdigest()
        self._write_build_manifest()
        with self.assertRaisesRegex(
            ReleaseProvenanceError,
            "manifest identity differs",
        ):
            self._generate()

    def test_deep_source_validation_rejects_resealed_wrong_archive_root(self) -> None:
        policy = deepcopy(self.source_policy)
        lock_document = deepcopy(self.lock_document)
        record = next(
            item
            for item in policy["archives"]
            if item["relationship"] == "magisk-cargo-registry"
        )
        package = record["cargo_packages"][0]
        payload = _source_tar_bytes(
            "wrong-source-root",
            files={
                "Cargo.toml": _cargo_manifest(
                    package["name"], package["version"], record["license"]
                )
            },
        )
        digest = hashlib.sha256(payload).hexdigest()
        record["size"] = len(payload)
        record["sha256"] = digest
        package["checksum"] = digest
        lock_record = lock_document["dependencies"][record["dependency"]]
        lock_record["size"] = len(payload)
        lock_record["sha256"] = digest
        lock_canonical_sha256 = _canonical_sha256(lock_document)

        policy_bytes = (json.dumps(policy, indent=2) + "\n").encode("utf-8")
        policy_path = (
            self.repository_root
            / "packaging"
            / "anykernel3"
            / "CORRESPONDING-SOURCE.json"
        )
        policy_path.write_bytes(policy_bytes)
        source_manifest = json.loads(self.source_manifest_bytes)
        source_manifest["archives"] = policy["archives"]
        source_manifest["dependency_lock_sha256"] = lock_canonical_sha256
        source_manifest["source_policy"]["sha256"] = hashlib.sha256(
            policy_bytes
        ).hexdigest()
        manifest_bytes = (
            json.dumps(source_manifest, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        members = {
            **self.source_payloads,
            record["archive_path"]: payload,
            "SOURCE-MANIFEST.json": manifest_bytes,
            "SOURCE-POLICY.json": policy_bytes,
        }
        companion = self.assets / "resealed-wrong-root-source.zip"
        _write_release_zip(
            companion,
            members,
            compression=zipfile.ZIP_STORED,
        )
        artifact_record = deepcopy(
            next(
                item
                for item in self.build_manifest["artifacts"]
                if item["role"] == "corresponding-source"
            )
        )
        artifact_record["source_policy_sha256"] = hashlib.sha256(
            policy_bytes
        ).hexdigest()
        artifact_record["source_manifest_sha256"] = hashlib.sha256(
            manifest_bytes
        ).hexdigest()
        with self.assertRaisesRegex(
            ReleaseProvenanceError,
            "source archive root differs",
        ):
            _validate_corresponding_source_companion(
                archive_path=companion,
                artifact_record=artifact_record,
                repository_root=self.repository_root,
                lock_document=lock_document,
                lock_canonical_sha256=lock_canonical_sha256,
                source_date_epoch=1783987200,
            )

    def test_rejects_resealed_source_companion_timestamp(self) -> None:
        self._reseal_companion_metadata(date_time=(2044, 1, 1, 0, 0, 0))
        with self.assertRaisesRegex(ReleaseProvenanceError, "member metadata"):
            self._generate()

    def test_rejects_resealed_source_companion_extra_field(self) -> None:
        self._reseal_companion_metadata(extra=b"\xfe\xca\x00\x00")
        with self.assertRaisesRegex(ReleaseProvenanceError, "member metadata"):
            self._generate()

    def test_rejects_resealed_source_companion_archive_comment(self) -> None:
        self._reseal_companion_metadata(archive_comment=b"resealed")
        with self.assertRaisesRegex(ReleaseProvenanceError, "comment is forbidden"):
            self._generate()

    def test_rejects_tampered_anykernel_tool_when_outer_hashes_are_resealed(self) -> None:
        with zipfile.ZipFile(self.anykernel, "r") as archive:
            members = {info.filename: archive.read(info) for info in archive.infolist()}
        members["tools/magiskboot"] += b"tampered"
        _write_release_zip(
            self.anykernel,
            members,
            modes=dict(ANYKERNEL_RELEASE_MODES),
        )
        record = next(
            item
            for item in self.build_manifest["artifacts"]
            if item["role"] == "anykernel3-zip"
        )
        record["size"] = self.anykernel.stat().st_size
        record["sha256"] = _sha256(self.anykernel)
        self._write_build_manifest()
        with self.assertRaisesRegex(
            ReleaseProvenanceError,
            "executable differs from its audited policy",
        ):
            self._generate()

    def test_rejects_tampered_anykernel_template_when_outer_hashes_are_resealed(self) -> None:
        with zipfile.ZipFile(self.anykernel, "r") as archive:
            members = {info.filename: archive.read(info) for info in archive.infolist()}
        members["tools/ak3-core.sh"] += b"# resealed mutation\n"
        _write_release_zip(
            self.anykernel,
            members,
            modes=dict(ANYKERNEL_RELEASE_MODES),
        )
        record = next(
            item
            for item in self.build_manifest["artifacts"]
            if item["role"] == "anykernel3-zip"
        )
        record["size"] = self.anykernel.stat().st_size
        record["sha256"] = _sha256(self.anykernel)
        self._write_build_manifest()
        with self.assertRaisesRegex(ReleaseProvenanceError, "pinned Git blob"):
            self._generate()

    def test_rejects_noncanonical_package_checksum_inventory(self) -> None:
        checksum_path = self.assets / "SHA256SUMS"
        lines = checksum_path.read_text(encoding="ascii").splitlines()
        checksum_path.write_text(
            "\n".join(
                line
                for line in lines
                if not line.endswith(f"  {self.companion.name}")
            )
            + "\n",
            encoding="ascii",
            newline="\n",
        )
        with self.assertRaisesRegex(
            ReleaseProvenanceError,
            "exact canonical asset inventory",
        ):
            self._generate()

    def test_rejects_extra_release_asset(self) -> None:
        (self.assets / "undeclared.bin").write_bytes(b"undeclared\n")
        with self.assertRaisesRegex(
            ReleaseProvenanceError,
            "release asset coverage differs",
        ):
            self._generate()

    def test_rejects_dependency_without_immutable_commit(self) -> None:
        mutated = deepcopy(self.build_manifest)
        del mutated["dependencies"][0]["source"]["commit"]
        self.build_manifest = mutated
        self._write_build_manifest()
        with self.assertRaisesRegex(ReleaseProvenanceError, "inventory differs"):
            self._generate()

    def test_release_workflow_uses_pinned_tooling_without_checkout_credentials(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("python3 scripts/generate-release-provenance.py", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertNotIn("python3 - <<'PY'", workflow)
        self.assertIn('[[ "$GITHUB_REF" != refs/heads/main', workflow)
        self.assertIn('! "$GITHUB_SHA" =~ ^[0-9a-f]{40}$', workflow)

    def test_release_workflow_forces_clean_rebuild_with_approval_safe_retention(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        dispatch_inputs = workflow.split("  workflow_dispatch:\n", 1)[1].split(
            "permissions:\n", 1
        )[0]
        self.assertNotIn("      clean:\n", dispatch_inputs)
        self.assertNotIn("inputs.clean", workflow)

        prerequisite = workflow.split("  module-kernel-prerequisite:\n", 1)[1].split(
            "\n  rebuild:\n", 1
        )[0]
        self.assertIn("      clean: true\n", prerequisite)
        self.assertIn("      artifact_retention_days: 35\n", prerequisite)

        rebuild = workflow.split("  rebuild:\n", 1)[1].split("\n  publish:\n", 1)[0]
        self.assertIn("      clean: true\n", rebuild)
        self.assertIn("      artifact_retention_days: 35\n", rebuild)
        self.assertIn("      CLEAN_BUILD: true\n", workflow)

    def test_release_metadata_is_captured_once_and_forwarded_exactly(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        validator = workflow.split("  validate-release-inputs:\n", 1)[1].split(
            "\n  module-kernel-prerequisite:\n", 1
        )[0]
        self.assertIn("      branding: ${{ steps.release-metadata.outputs.branding }}", validator)
        self.assertIn(
            "      build_timestamp: ${{ steps.release-metadata.outputs.build_timestamp }}",
            validator,
        )
        self.assertEqual(workflow.count("${{ vars.BUILD_TIMESTAMP || '' }}"), 1)
        self.assertEqual(
            workflow.count("${{ vars.KERNEL_BRANDING || 'OnePlus13-KernelBuilder' }}"),
            1,
        )
        self.assertEqual(
            workflow.count("${{ needs.validate-release-inputs.outputs.build_timestamp }}"),
            3,
        )
        self.assertEqual(
            workflow.count("${{ needs.validate-release-inputs.outputs.branding }}"),
            3,
        )


if __name__ == "__main__":
    unittest.main()
