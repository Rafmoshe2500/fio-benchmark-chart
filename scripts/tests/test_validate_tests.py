import io
import json
import os
import re
import shutil
import tempfile
import unittest

import validate_tests as vt

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class RealRepoTest(unittest.TestCase):
    """The repository as it stands must be internally consistent.

    This is the check that was missing. Every profile in jobs/profiles
    declared its own pod count while deploy_test.sh hardcoded 10, so the
    `characterise` suite deployed 10 pods against metadata expecting 4 and
    every one of its eight tests was rejected -- after the suite had run for
    hours and the measurement was already spent.
    """

    def test_every_test_is_consistent(self):
        found = vt.discover()
        sizes = vt.pvc_sizes()
        broken = {}
        for test_id, job_dir in sorted(found.items()):
            problems = vt.check(test_id, job_dir, sizes)
            if problems:
                broken[test_id] = problems
        self.assertEqual(
            broken, {},
            "\n" + "\n".join("%s: %s" % (k, "; ".join(v))
                             for k, v in sorted(broken.items())))

    def test_every_suite_names_tests_that_exist(self):
        """A typo in suites.json is a mid-suite failure otherwise."""
        with io.open(os.path.join(REPO, "scripts", "suites.json"),
                     encoding="utf-8") as fh:
            suites = json.load(fh)["suites"]
        found = vt.discover()
        missing = {name: [t for t in tests if t not in found]
                   for name, tests in suites.items()}
        missing = {k: v for k, v in missing.items() if v}
        self.assertEqual(missing, {})


class DeployTestReplicasTest(unittest.TestCase):
    """deploy_test.sh must not restate a pod count that metadata already owns.

    A structural check rather than a behavioural one: the two numbers cannot
    disagree if only one of them exists.
    """

    def _deploy_script(self):
        with io.open(os.path.join(REPO, "scripts", "deploy_test.sh"),
                     encoding="utf-8") as fh:
            return fh.read()

    def test_no_hardcoded_replica_count(self):
        text = self._deploy_script()
        # deploy_release <suffix> <job_id> -- a bare integer in the second
        # position is the old signature and the bug.
        offenders = re.findall(r'^\s*deploy_release\s+("[^"]*")\s+(\d+)',
                               text, re.M)
        self.assertEqual(
            offenders, [],
            "deploy_release is being passed a literal pod count: %s. "
            "It must come from replicas_for(), which reads the same "
            ".meta.json parse_results.py validates the run against."
            % offenders)

    def test_replicas_come_from_metadata(self):
        self.assertIn("replicas_for", self._deploy_script())

    def test_common_sh_defines_replicas_for(self):
        with io.open(os.path.join(REPO, "scripts", "lib", "common.sh"),
                     encoding="utf-8") as fh:
            self.assertIn("replicas_for()", fh.read())


class CheckTest(unittest.TestCase):
    """Each inconsistency validate_tests.py exists to catch, injected."""

    FIO = (
        "[global]\n"
        "ioengine=libaio\n"
        "direct=1\n"
        "size=1G\n"
        "runtime=600\n"
        "ramp_time=60\n"
        "time_based=1\n"
        "numjobs=2\n"
        "iodepth=8\n"
        "directory=/mnt/fio-data\n"
        "\n"
        "[job]\n"
        "rw=randrw\n"
        "rwmixread=70\n"
        "bs=4k\n"
    )

    META = {
        "test_id": "t",
        "replicas": 4,
        "numjobs": 2,
        "expected_directions": ["read", "write"],
        "runtime_s": 600,
        "ramp_s": 60,
        "rate_limited": False,
    }

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d)

    def _write(self, fio=None, **meta_over):
        meta = dict(self.META)
        meta.update(meta_over)
        with io.open(os.path.join(self.d, "t.fio"), "w",
                     encoding="utf-8", newline="\n") as fh:
            fh.write(fio if fio is not None else self.FIO)
        with io.open(os.path.join(self.d, "t.meta.json"), "w",
                     encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(meta))

    def _check(self, sizes=None):
        return vt.check("t", self.d, sizes if sizes is not None else {"t": "8Gi"})

    def test_consistent_test_has_no_problems(self):
        self._write()
        self.assertEqual(self._check(), [])

    def test_replica_mismatch_is_not_this_check(self):
        """replicas has one home now, so there is nothing to cross-check --
        only that the value is usable."""
        self._write(replicas=0)
        self.assertIn("replicas", " ".join(self._check()))

    def test_numjobs_mismatch(self):
        self._write(numjobs=8)
        self.assertIn("numjobs", " ".join(self._check()))

    def test_runtime_mismatch(self):
        """The job runs 600s, the metadata claims 1800; a healthy run is
        rejected for 'ran 660s, expected at least 1620s'."""
        self._write(runtime_s=1800)
        joined = " ".join(self._check())
        self.assertIn("runtime", joined)
        self.assertIn("1800", joined)

    def test_ramp_mismatch(self):
        self._write(ramp_s=30)
        self.assertIn("ramp_time", " ".join(self._check()))

    def test_direction_the_job_cannot_produce(self):
        """rw=randread with rwmixread irrelevant: no write will ever appear,
        so 'no I/O in expected direction write' rejects every healthy run."""
        fio = self.FIO.replace("rw=randrw", "rw=randread")
        self._write(fio=fio)
        joined = " ".join(self._check())
        self.assertIn("expected_directions", joined)
        self.assertIn("write", joined)

    def test_rwmixread_100_produces_no_writes(self):
        """randrw is still one-directional at the extremes; fiojob reports
        both because that is right for capacity, not for this."""
        fio = self.FIO.replace("rwmixread=70", "rwmixread=100")
        self._write(fio=fio)
        self.assertIn("expected_directions", " ".join(self._check()))

    def test_rwmixread_100_with_read_only_meta_is_fine(self):
        fio = self.FIO.replace("rwmixread=70", "rwmixread=100")
        self._write(fio=fio, expected_directions=["read"])
        self.assertEqual(self._check(), [])

    def test_unregistered_pvc_size(self):
        self._write()
        self.assertIn("pvc_sizes.conf", " ".join(self._check(sizes={})))

    def test_pvc_too_small_is_the_enospc_case(self):
        """size=1G x numjobs=2 needs 2.4Gi with headroom."""
        self._write()
        joined = " ".join(self._check(sizes={"t": "2Gi"}))
        self.assertIn("too small", joined)
        self.assertIn("ENOSPC", joined)

    def test_missing_metadata(self):
        with io.open(os.path.join(self.d, "t.fio"), "w",
                     encoding="utf-8", newline="\n") as fh:
            fh.write(self.FIO)
        self.assertIn("no metadata", " ".join(self._check()))

    def test_meta_test_id_must_match_the_file_name(self):
        self._write(test_id="something_else")
        self.assertIn("test_id", " ".join(self._check()))


class MainTest(unittest.TestCase):
    def test_exit_zero_on_the_real_repo(self):
        self.assertEqual(vt.main(["--quiet"]), 0)

    def test_unknown_test_is_an_error(self):
        self.assertEqual(vt.main(["--quiet", "no_such_test"]), 1)


if __name__ == "__main__":
    unittest.main()
