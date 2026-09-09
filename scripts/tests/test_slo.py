import json
import os
import tempfile
import unittest

from lib.fiojson import DirectionResult, PodResult
from lib.slo import SloError, diagnose, load_slo, verdict


def _pod(name, r_iops=1000.0, r_p99=5.0, w_iops=1000.0, w_p99=5.0,
         usr_cpu=10.0, throttled=0):
    p = PodResult(
        pod=name, error=0, elapsed_s=600, usr_cpu=usr_cpu, sys_cpu=5.0,
        read=DirectionResult("read", iops=r_iops, io_bytes=1, runtime_ms=600000,
                             bw_mibps=100.0, clat_mean_ms=1.0, clat_p99_ms=r_p99),
        write=DirectionResult("write", iops=w_iops, io_bytes=1, runtime_ms=600000,
                              bw_mibps=100.0, clat_mean_ms=1.0, clat_p99_ms=w_p99))
    p.throttled_usec = throttled
    return p


class LoadSloTest(unittest.TestCase):
    def _write(self, obj):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as fh:
            json.dump(obj, fh)
        self.addCleanup(os.unlink, path)
        return path

    def test_absent_file_is_not_an_error(self):
        """No SLO file simply means no verdict, not a crash."""
        self.assertIsNone(load_slo("/nonexistent/slo.json"))

    def test_uncalibrated_slo_is_refused(self):
        """A template nobody has filled in must not silently pass runs."""
        path = self._write({"calibrated": False, "source": "TEMPLATE",
                            "targets": {}})
        with self.assertRaises(SloError) as cm:
            load_slo(path)
        self.assertIn("not calibrated", str(cm.exception))

    def test_calibrated_slo_requires_a_source(self):
        path = self._write({"calibrated": True, "targets": {"x": {"max_p99_ms": 5}}})
        with self.assertRaises(SloError) as cm:
            load_slo(path)
        self.assertIn("source", str(cm.exception))

    def test_loads_calibrated_slo(self):
        path = self._write({"calibrated": True, "source": "prod-nifi 2026-09",
                            "targets": {"t1": {"max_p99_ms": 20.0}}})
        self.assertEqual(load_slo(path)["targets"]["t1"]["max_p99_ms"], 20.0)


class VerdictTest(unittest.TestCase):
    AGG = {"total_iops": 20000.0, "total_bw_mibps": 2000.0,
           "read_p99_worst_ms": 8.0, "write_p99_worst_ms": 9.0}

    def test_no_slo_gives_no_verdict(self):
        v = verdict(self.AGG, None, "t1")
        self.assertIsNone(v["pass"])
        self.assertIn("no SLO", v["reason"])

    def test_no_entry_for_this_test_gives_no_verdict(self):
        slo = {"calibrated": True, "source": "s", "targets": {"other": {}}}
        self.assertIsNone(verdict(self.AGG, slo, "t1")["pass"])

    def test_passes_when_within_every_threshold(self):
        slo = {"calibrated": True, "source": "s",
               "targets": {"t1": {"min_iops": 15000, "max_p99_ms": 20.0}}}
        v = verdict(self.AGG, slo, "t1")
        self.assertTrue(v["pass"])
        self.assertEqual(v["breaches"], [])

    def test_fails_and_quotes_the_threshold_crossed(self):
        slo = {"calibrated": True, "source": "s",
               "targets": {"t1": {"min_iops": 30000, "max_p99_ms": 5.0}}}
        v = verdict(self.AGG, slo, "t1")
        self.assertFalse(v["pass"])
        joined = " ".join(v["breaches"])
        self.assertIn("30,000", joined)
        self.assertIn("5.0", joined)


class DiagnoseTest(unittest.TestCase):
    def test_client_cpu_is_claimed_only_with_cpu_evidence(self):
        f = diagnose([_pod("p0", usr_cpu=93.0)], {})
        self.assertTrue(any("client CPU" in x for x in f))

    def test_no_claim_without_evidence(self):
        """The old parser asserted NFS lock contention and server GC from fio
        output alone. Nothing in fio can establish either."""
        f = diagnose([_pod("p0")], {})
        self.assertEqual(f, [])

    def test_retransmits_support_a_network_claim(self):
        f = diagnose([_pod("p0")], {"nfs": {"WRITE": {"retrans_pct": 7.5,
                                                     "timeouts": 12}}})
        self.assertTrue(any("retransmit" in x.lower() for x in f))

    def test_throttling_is_reported_as_client_side(self):
        f = diagnose([_pod("p0", throttled=3_000_000)], {})
        self.assertTrue(any("throttl" in x.lower() for x in f))

    def test_never_claims_lock_contention_or_server_gc(self):
        f = diagnose([_pod("p0", r_p99=900.0, w_p99=900.0)], {})
        joined = " ".join(f).lower()
        self.assertNotIn("lock", joined)
        self.assertNotIn("gc", joined)


if __name__ == "__main__":
    unittest.main()
