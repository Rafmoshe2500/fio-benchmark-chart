import json
import os
import shutil
import tempfile
import unittest

from lib.envcompare import CompareError, compare_suites, load_suite


def _suite(root, suite_id, env, tests, values, suite="quick",
           commit="abc123", repeats=3):
    """values: {test_id: [total_iops per repeat]}"""
    d = os.path.join(root, suite_id)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "suite.json"), "w") as fh:
        json.dump({"suite_id": suite_id, "suite": suite, "environment": env,
                   "storage_class": "sc-" + env, "tests": tests,
                   "repeats": repeats, "git_commit": commit}, fh)
    for t, reps in values.items():
        td = os.path.join(d, t)
        os.makedirs(td, exist_ok=True)
        for i, iops in enumerate(reps, start=1):
            with open(os.path.join(td, "rep-%d.json" % i), "w") as fh:
                json.dump({"valid": True, "test_id": t,
                           "aggregate": {"total_iops": iops,
                                         "total_bw_mibps": iops / 100.0,
                                         "read_p99_worst_ms": 5.0,
                                         "write_p99_worst_ms": 6.0}}, fh)
    return d


class LoadSuiteTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)

    def test_loads_values(self):
        d = _suite(self.root, "s1", "nfs3", ["t1"], {"t1": [100.0, 102.0, 98.0]})
        s = load_suite(d)
        self.assertEqual(s["environment"], "nfs3")
        self.assertEqual(len(s["results"]["t1"]["total_iops"]), 3)

    def test_missing_suite_json(self):
        d = os.path.join(self.root, "empty")
        os.makedirs(d)
        with self.assertRaises(CompareError):
            load_suite(d)

    def test_rejected_repeats_are_excluded(self):
        d = _suite(self.root, "s1", "nfs3", ["t1"], {"t1": [100.0, 102.0]})
        with open(os.path.join(d, "t1", "rep-3.json"), "w") as fh:
            json.dump({"valid": False, "aggregate": {}}, fh)
        self.assertEqual(len(load_suite(d)["results"]["t1"]["total_iops"]), 2)


class CompareSuitesTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)

    def test_refuses_different_suites(self):
        a = _suite(self.root, "a", "nfs3", ["t1"], {"t1": [100.0]}, suite="quick")
        b = _suite(self.root, "b", "nfs41", ["t1"], {"t1": [100.0]}, suite="nifi")
        with self.assertRaises(CompareError) as cm:
            compare_suites([load_suite(a), load_suite(b)])
        self.assertIn("different suites", str(cm.exception))

    def test_refuses_same_name_with_different_test_lists(self):
        """'manual' edited between runs: the name matches but the measurement
        is not the same one."""
        a = _suite(self.root, "a", "nfs3", ["t1", "t2"],
                   {"t1": [100.0, 101.0, 99.0], "t2": [50.0, 51.0, 49.0]},
                   suite="manual")
        b = _suite(self.root, "b", "nfs41", ["t1"],
                   {"t1": [100.0, 101.0, 99.0]}, suite="manual")
        with self.assertRaises(CompareError) as cm:
            compare_suites([load_suite(a), load_suite(b)])
        self.assertIn("different test lists", str(cm.exception))

    def test_warns_on_different_commits(self):
        a = _suite(self.root, "a", "nfs3", ["t1"], {"t1": [100.0, 101.0, 99.0]}, commit="aaa")
        b = _suite(self.root, "b", "nfs41", ["t1"], {"t1": [100.0, 101.0, 99.0]}, commit="bbb")
        r = compare_suites([load_suite(a), load_suite(b)])
        self.assertTrue(any("commit" in w for w in r["warnings"]))

    def test_overlapping_intervals_report_no_difference(self):
        """The case this tool exists for: a few percent apart with noise
        larger than the gap is NOT a difference."""
        a = _suite(self.root, "a", "nfs3", ["t1"], {"t1": [100.0, 104.0, 96.0]})
        b = _suite(self.root, "b", "nfs41", ["t1"], {"t1": [103.0, 107.0, 99.0]})
        r = compare_suites([load_suite(a), load_suite(b)])
        row = r["rows"][0]
        self.assertFalse(row["established"])
        self.assertIn("no difference", row["verdict"])

    def test_separated_intervals_report_a_difference(self):
        a = _suite(self.root, "a", "nfs3", ["t1"], {"t1": [100.0, 101.0, 99.0]})
        b = _suite(self.root, "b", "nfs41", ["t1"], {"t1": [200.0, 201.0, 199.0]})
        r = compare_suites([load_suite(a), load_suite(b)])
        row = r["rows"][0]
        self.assertTrue(row["established"])
        self.assertIn("nfs41", row["verdict"])

    def test_single_repeat_can_never_establish_a_difference(self):
        a = _suite(self.root, "a", "nfs3", ["t1"], {"t1": [100.0]}, repeats=1)
        b = _suite(self.root, "b", "nfs41", ["t1"], {"t1": [400.0]}, repeats=1)
        r = compare_suites([load_suite(a), load_suite(b)])
        self.assertFalse(r["rows"][0]["established"])
        self.assertTrue(any("repeat" in w.lower() for w in r["warnings"]))

    def test_test_missing_from_every_environment_is_still_reported(self):
        """A real nfs3-vs-nfs41 comparison declared six tests; two failed to
        deploy in both environments, so they appeared in neither set of
        results and the table was built from the results alone. The
        comparison presented four tests as though four had been asked for.
        """
        a = _suite(self.root, "a", "nfs3", ["t1", "t_dead"],
                   {"t1": [100.0, 101.0, 99.0]})
        b = _suite(self.root, "b", "nfs41", ["t1", "t_dead"],
                   {"t1": [100.0, 101.0, 99.0]})
        r = compare_suites([load_suite(a), load_suite(b)])
        joined = " ".join(r["warnings"])
        self.assertIn("t_dead", joined)
        self.assertIn("ANY environment", joined)
        self.assertNotIn("t_dead", [row["test_id"] for row in r["rows"]])

    def test_test_missing_from_one_environment_is_flagged(self):
        a = _suite(self.root, "a", "nfs3", ["t1", "t2"],
                   {"t1": [100.0, 101.0, 99.0], "t2": [50.0, 51.0, 49.0]})
        b = _suite(self.root, "b", "nfs41", ["t1", "t2"], {"t1": [100.0, 101.0, 99.0]})
        r = compare_suites([load_suite(a), load_suite(b)])
        self.assertTrue(any("t2" in w for w in r["warnings"]))


if __name__ == "__main__":
    unittest.main()
