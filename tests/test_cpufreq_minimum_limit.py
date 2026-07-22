from __future__ import annotations

import hashlib
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / "patches" / "oneplus13" / "0006-cpufreq-minimum-limit-oneplus-6.6.patch"


class CpufreqMinimumLimitTests(unittest.TestCase):
    def test_patch_is_exact_and_keeps_storage_distinct_from_sysfs_attribute(self) -> None:
        payload = PATCH.read_bytes()
        self.assertEqual(len(payload), 3218)
        self.assertEqual(
            hashlib.sha256(payload).hexdigest(),
            "47597af452b0b8f9dd833ad5e1292c643d3147e99c3bd3909a3e9828239fe303",
        )
        text = payload.decode("utf-8")
        added = "\n".join(
            line[1:]
            for line in text.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )

        array_names = set(
            re.findall(r"static unsigned int\s+([a-zA-Z0-9_]+)\[", added)
        )
        attribute_names = set(
            re.findall(r"cpufreq_freq_attr_rw\(([a-zA-Z0-9_]+)\)", added)
        )
        self.assertEqual(array_names, {"scaling_min_freq_limit_store"})
        self.assertEqual(attribute_names, {"scaling_min_freq_limit"})
        self.assertTrue(array_names.isdisjoint(attribute_names))
        self.assertEqual(added.count("scaling_min_freq_limit_store["), 4)
        self.assertEqual(added.count("cpufreq_freq_attr_rw(scaling_min_freq_limit)"), 1)
        self.assertEqual(added.count("&scaling_min_freq_limit.attr"), 1)
        self.assertNotIn("scaling_min_freq_limit_store.attr", added)
        self.assertNotIn("cpufreq_freq_attr_rw(scaling_min_freq_limit_store)", added)

    def test_series_fans_the_fix_to_both_locked_kernel_trees(self) -> None:
        import sys

        sys.path.insert(0, str(ROOT / "scripts"))
        from lib.config import discover_configs
        from lib.patches import _load_series, _operation_enabled

        _, _, profiles, features = discover_configs(ROOT)
        _, operations = _load_series(ROOT / "patches" / "series" / "wild.yml")
        operation = next(
            item for item in operations if item["id"] == "cpufreq-minimum-limit-hook"
        )
        self.assertEqual(operation["kernel_trees"], ["common", "msm-kernel"])
        self.assertEqual(
            operation["path"],
            "patches/oneplus13/0006-cpufreq-minimum-limit-oneplus-6.6.patch",
        )
        enabled = {
            (feature.id, base)
            for feature in features.values()
            for base in profiles
            if _operation_enabled(operation, feature, base, "kernelsu-next")
        }
        self.assertEqual(
            enabled,
            {
                (feature, base)
                for feature in ("full", "wild")
                for base in ("oos15-cn", "oos15-global", "oos16")
            },
        )


if __name__ == "__main__":
    unittest.main()
