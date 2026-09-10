import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(SCRIPTS)
FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


class SteppedRunTest(unittest.TestCase):
    """A stepped run's pod count comes from the step, not the metadata.

    test12..test16 measure one job at 10, 20, 40 and 80 pods, each step a
    complete run in results/<run>/step-<n>/. The metadata can only declare one
    number, so three of the four steps were rejected for "expected 10 pods,
    got 40" -- after a four-step curve had already taken two hours.
    """

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d)
        with io.open(os.path.join(FIX, "ok_mixed.json"), encoding="utf-8") as fh:
            self.fio_json = fh.read()

    def _run_dir(self, test_id, step, pods):
        run = os.path.join(self.d, "run-1")
        os.makedirs(run)
        with io.open(os.path.join(run, "manifest.json"), "w",
                     encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps({"run_id": "run-1", "test_id": test_id,
                                 "stepped": True}))
        step_dir = os.path.join(run, "step-%d" % step)
        os.makedirs(step_dir)
        for i in range(pods):
            with io.open(os.path.join(step_dir, "pod-%d.log" % i), "w",
                         encoding="utf-8", newline="\n") as fh:
                fh.write("===FIO_JSON_BEGIN===\n%s\n===FIO_JSON_END===\n"
                         % self.fio_json)
        return step_dir

    def _parse(self, step_dir):
        return subprocess.run(
            [sys.executable, "parse_results.py", step_dir],
            cwd=SCRIPTS, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True)

    def test_step_of_forty_is_accepted_despite_metadata_saying_ten(self):
        d = self._run_dir("test12_gradual_scale_32kb_7030", step=40, pods=40)
        r = self._parse(d)
        self.assertNotIn("expected 10 pods", r.stdout)
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_a_step_missing_pods_is_still_rejected(self):
        """The check is relaxed to the step's count, not removed."""
        d = self._run_dir("test12_gradual_scale_32kb_7030", step=40, pods=39)
        r = self._parse(d)
        self.assertIn("expected 40 pods, got 39", r.stdout)
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
