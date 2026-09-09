import json
import os
import shutil
import tempfile
import unittest

from lib.envcompare import load_suite
from lib.suitereport import summarise_suite


def _build(root, tests_values, repeats=3, rejected=0):
    d = os.path.join(root, "quick-nfs3-demo")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "suite.json"), "w") as fh:
        json.dump({"suite_id": "quick-nfs3-demo", "suite": "quick",
                   "environment": "nfs3", "storage_class": "sc-nas-nfs3",
                   "tests": list(tests_values), "repeats": repeats,
                   "git_commit": "abc1234", "git_dirty": False,
                   "passed": sum(len(v) for v in tests_values.values()),
                   "rejected": rejected}, fh)
    for t, reps in tests_values.items():
        td = os.path.join(d, t)
        os.makedirs(td, exist_ok=True)
        for i, v in enumerate(reps, start=1):
            with open(os.path.join(td, "rep-%d.json" % i), "w") as fh:
                json.dump({"valid": True, "test_id": t,
                           "aggregate": {"total_iops": v,
                                         "total_bw_mibps": v / 100.0,
                                         "read_p99_worst_ms": 5.0,
                                         "write_p99_worst_ms": 6.0}}, fh)
    return d


class SummariseSuiteTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)

    def test_reports_median_and_interval_per_test(self):
        d = _build(self.root, {"t1": [100.0, 104.0, 96.0]})
        r = summarise_suite(load_suite(d))
        row = [x for x in r["rows"] if x["metric"] == "total_iops"][0]
        self.assertEqual(row["n"], 3)
        self.assertEqual(row["median"], 100.0)
        self.assertIsNotNone(row["ci95_lo"])

    def test_single_repeat_has_no_interval(self):
        """One run has no spread; reporting +/-0 would imply a precision the
        run cannot support."""
        d = _build(self.root, {"t1": [100.0]}, repeats=1)
        r = summarise_suite(load_suite(d))
        row = [x for x in r["rows"] if x["metric"] == "total_iops"][0]
        self.assertIsNone(row["ci95_lo"])
        self.assertTrue(any("repeat" in w.lower() for w in r["warnings"]))

    def test_rejected_runs_are_surfaced(self):
        d = _build(self.root, {"t1": [100.0, 101.0]}, repeats=3, rejected=1)
        r = summarise_suite(load_suite(d))
        self.assertTrue(any("rejected" in w.lower() for w in r["warnings"]))

    def test_test_with_no_valid_runs_is_listed(self):
        d = _build(self.root, {"t1": [100.0, 101.0, 99.0]})
        # declare a second test that produced nothing
        sj = os.path.join(d, "suite.json")
        s = json.load(open(sj))
        s["tests"].append("t2")
        json.dump(s, open(sj, "w"))
        r = summarise_suite(load_suite(d))
        self.assertIn("t2", r["missing"])

    def test_spread_is_reported_as_a_percentage(self):
        d = _build(self.root, {"t1": [100.0, 110.0, 90.0]})
        r = summarise_suite(load_suite(d))
        row = [x for x in r["rows"] if x["metric"] == "total_iops"][0]
        self.assertGreater(row["spread_pct"], 0.0)


if __name__ == "__main__":
    unittest.main()
