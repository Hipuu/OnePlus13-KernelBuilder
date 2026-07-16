from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lib.patches import _expand_kernel_tree_operation, _load_series


PATCH = ROOT / "patches" / "oneplus13" / "hmbird" / "cpufreq-api-oos15-cn.patch"
SERIES = ROOT / "patches" / "series" / "wild.yml"


class HmbirdCpufreqCompatibilityTests(unittest.TestCase):
    def test_exact_upstream_hunk_is_pinned(self) -> None:
        payload = PATCH.read_bytes()
        self.assertEqual(
            hashlib.sha256(payload).hexdigest(),
            "8797b202bfaba5ffb04e9b35a61e9bb9addf73aeb71abb50e7f4e5ffa3555604",
        )
        text = payload.decode("utf-8")
        self.assertIn(
            "index 63452733a2f9..3a2f906ed3ce 100644\n",
            text,
        )
        self.assertEqual(text.count("store_scaling_governor"), 1)
        self.assertEqual(text.count("show_scaling_governor"), 1)
        self.assertNotIn("drivers/cpufreq/cpufreq.c", text)

    def test_operation_is_china_common_only_and_ordered_after_fengchi(self) -> None:
        series_id, operations = _load_series(SERIES)
        self.assertEqual(series_id, "wild")
        operation_by_id = {operation["id"]: operation for operation in operations}
        operation = operation_by_id["hmbird-cpufreq-api-oos15-cn"]

        self.assertEqual(operation["bases"], ["oos15-cn"])
        self.assertEqual(operation["cwd"], "kernel_platform/common")
        self.assertEqual(operation["strip"], 1)
        self.assertEqual(
            operation["path"],
            "patches/oneplus13/hmbird/cpufreq-api-oos15-cn.patch",
        )
        self.assertNotIn("kernel_trees", operation)
        self.assertEqual(_expand_kernel_tree_operation(operation), [operation])

        ids = [operation["id"] for operation in operations]
        self.assertLess(ids.index("hmbird-fengchi-oneplus13"), ids.index(operation["id"]))
        self.assertLess(
            ids.index("hmbird-fengchi-oneplus13-vendor"),
            ids.index(operation["id"]),
        )
        self.assertLess(ids.index(operation["id"]), ids.index("hmbird-device-tree-overwriter"))

    def test_patch_adds_declarations_without_rewriting_cpufreq_implementation(self) -> None:
        before = (
            "struct cpufreq_governor;\n"
            "\n"
            "enum cpufreq_table_sorting {\n"
            "\tCPUFREQ_TABLE_UNSORTED,\n"
            "\tCPUFREQ_TABLE_SORTED_ASCENDING,\n"
            "\tCPUFREQ_TABLE_SORTED_DESCENDING\n"
            "};\n"
            "\n"
            "struct cpufreq_cpuinfo {\n"
            "\tunsigned int\t\tmax_freq;\n"
            "\tunsigned int\t\tmin_freq;\n"
            "};\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            tree = Path(temporary)
            header = tree / "include" / "linux" / "cpufreq.h"
            header.parent.mkdir(parents=True)
            header.write_text(before, encoding="utf-8", newline="\n")
            subprocess.run(
                ["git", "init", "-q"],
                cwd=tree,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            subprocess.run(
                ["git", "apply", "--check", "-p1", str(PATCH)],
                cwd=tree,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            subprocess.run(
                ["git", "apply", "-p1", str(PATCH)],
                cwd=tree,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            after = header.read_text(encoding="utf-8")
            self.assertIn(
                "ssize_t store_scaling_governor(struct cpufreq_policy *policy,\n"
                "                                        const char *buf, size_t count);\n",
                after,
            )
            self.assertIn(
                "ssize_t show_scaling_governor(struct cpufreq_policy *policy, char *buf);\n",
                after,
            )
            self.assertEqual(after.count("store_scaling_governor"), 1)
            self.assertEqual(after.count("show_scaling_governor"), 1)


if __name__ == "__main__":
    unittest.main()
