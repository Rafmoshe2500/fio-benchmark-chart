import json
import os
import tempfile
import unittest

from lib.testmeta import MetaError, load_meta


class LoadMetaTest(unittest.TestCase):
    def _dir(self, obj, name="t1"):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, name + ".meta.json"), "w") as fh:
            json.dump(obj, fh)
        return d

    def _valid(self, **over):
        m = {"test_id": "t1", "replicas": 10, "numjobs": 8,
             "expected_directions": ["read", "write"],
             "runtime_s": 600, "rate_limited": True,
             "target_iops_per_pod": 3000}
        m.update(over)
        return m

    def test_loads_valid_meta(self):
        m = load_meta("t1", self._dir(self._valid()))
        self.assertEqual(m["replicas"], 10)

    def test_missing_file_is_an_error_not_a_default(self):
        """Guessing a profile from a directory name is how a 2,000 IOPS job
        got scored against a 15,000 IOPS target. Intent must be declared."""
        with self.assertRaises(MetaError) as cm:
            load_meta("nope", tempfile.mkdtemp())
        self.assertIn("no metadata", str(cm.exception))

    def test_missing_required_field_rejected(self):
        d = self._dir({"test_id": "t1", "replicas": 10})
        with self.assertRaises(MetaError) as cm:
            load_meta("t1", d)
        self.assertIn("expected_directions", str(cm.exception))

    def test_bad_direction_rejected(self):
        d = self._dir(self._valid(expected_directions=["sideways"]))
        with self.assertRaises(MetaError):
            load_meta("t1", d)

    def test_empty_directions_rejected(self):
        d = self._dir(self._valid(expected_directions=[]))
        with self.assertRaises(MetaError):
            load_meta("t1", d)

    def test_invalid_json_rejected(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "t1.meta.json"), "w") as fh:
            fh.write("{not json")
        with self.assertRaises(MetaError) as cm:
            load_meta("t1", d)
        self.assertIn("invalid JSON", str(cm.exception))


class RealMetaFilesTest(unittest.TestCase):
    """Every shipped test must have metadata that loads."""

    def test_all_shipped_tests_have_loadable_metadata(self):
        import glob
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        jobs = os.path.join(os.path.dirname(here), "jobs", "tests")
        fios = sorted(glob.glob(os.path.join(jobs, "*.fio")))
        self.assertTrue(fios, "no job files found at " + jobs)
        for f in fios:
            tid = os.path.basename(f)[:-4]
            m = load_meta(tid, jobs)
            self.assertEqual(m["test_id"], tid)


if __name__ == "__main__":
    unittest.main()
