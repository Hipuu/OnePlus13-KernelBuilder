from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


class KconfigSurfaceContractTests(unittest.TestCase):
    def test_susfs_v220_fragments_do_not_request_removed_legacy_toggles(self) -> None:
        stale = {
            "CONFIG_KSU_SUSFS_HAS_MAGIC_MOUNT",
            "CONFIG_KSU_SUSFS_AUTO_ADD_SUS_KSU_DEFAULT_MOUNT",
            "CONFIG_KSU_SUSFS_AUTO_ADD_SUS_BIND_MOUNT",
            "CONFIG_KSU_SUSFS_SUS_OVERLAYFS",
            "CONFIG_KSU_SUSFS_TRY_UMOUNT",
            "CONFIG_KSU_SUSFS_AUTO_ADD_TRY_UMOUNT_FOR_BIND_MOUNT",
            "CONFIG_KSU_SUSFS_SUS_SU",
        }
        files = [
            ROOT / "patches" / "common" / "config-wild.config",
            ROOT / "patches" / "nethunter" / "config-nethunter-base.config",
            ROOT / "configs" / "features" / "wild.yml",
            ROOT / "configs" / "features" / "nethunter.yml",
            ROOT / "configs" / "features" / "full.yml",
        ]
        for path in files:
            text = path.read_text(encoding="utf-8")
            for symbol in stale:
                self.assertNotIn(symbol, text, f"{path} still requests {symbol}")

    def test_reject_targets_use_real_ipv4_and_ipv6_symbols(self) -> None:
        for relative in (
            "patches/common/config-wild.config",
            "patches/nethunter/config-nethunter-base.config",
        ):
            text = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("CONFIG_IP_NF_TARGET_REJECT=y", text)
            self.assertIn("CONFIG_IP6_NF_TARGET_REJECT=y", text)
            self.assertNotIn("CONFIG_NETFILTER_XT_TARGET_REJECT", text)

    def test_modular_usb_serial_does_not_request_builtin_only_console(self) -> None:
        text = (
            ROOT / "patches" / "nethunter" / "config-nethunter-common.config"
        ).read_text(encoding="utf-8")
        self.assertIn("CONFIG_USB_SERIAL=m", text)
        self.assertNotIn("CONFIG_USB_SERIAL_CONSOLE", text)

    def test_sdr_tuner_closure_pins_media_subdriver_autoselection_off(self) -> None:
        fragment = (
            ROOT / "patches" / "nethunter" / "config-nethunter-common.config"
        ).read_text(encoding="utf-8")
        self.assertEqual(
            fragment.count("# CONFIG_MEDIA_SUBDRV_AUTOSELECT is not set"),
            1,
        )
        for profile in ("nethunter", "full"):
            with self.subTest(profile=profile):
                data = json.loads(
                    (ROOT / "configs" / "features" / f"{profile}.yml").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(
                    data["required_symbols"]["CONFIG_MEDIA_SUBDRV_AUTOSELECT"],
                    "n",
                )

    def test_patch_rehearsal_preserves_failed_kconfig_evidence(self) -> None:
        text = (ROOT / ".github" / "workflows" / "validate.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("Collect failed Kconfig diagnostics", text)
        self.assertIn("out/debug/resolved-kconfig.txt", text)
        self.assertIn("out/debug/config-request.json", text)
        self.assertIn("out/debug/canonical-gki-defconfig.txt", text)


if __name__ == "__main__":
    unittest.main()
