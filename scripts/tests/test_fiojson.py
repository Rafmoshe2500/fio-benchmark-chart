import os
import unittest

from lib.fiojson import (MissingMetric, parse_cgroup_throttling,
                         parse_pod_json, validate_run)

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def _fixture(name):
    with open(os.path.join(FIX, name)) as fh:
        return fh.read()


class ParsePodJsonTest(unittest.TestCase):
    def test_healthy_mixed_run(self):
        r = parse_pod_json("pod-0", _fixture("ok_mixed.json"))
        self.assertEqual(r.error, 0)
        self.assertAlmostEqual(r.read.iops, 29980.5)
        self.assertAlmostEqual(r.read.clat_p99_ms, 9.0)
        self.assertAlmostEqual(r.write.clat_p99_ms, 9.2)
        self.assertEqual(r.directions_with_io, {"read", "write"})

    def test_nanoseconds_convert_to_milliseconds(self):
        """clat_ns is nanoseconds. The old text parser only knew usec and
        msec, so a fast device's latency was off by a factor of a million."""
        r = parse_pod_json("pod-0", _fixture("nsec_latency.json"))
        self.assertAlmostEqual(r.read.clat_mean_ms, 0.048)
        self.assertAlmostEqual(r.read.clat_p99_ms, 0.088)

    def test_absent_direction_is_none_not_zero(self):
        r = parse_pod_json("pod-0", _fixture("nsec_latency.json"))
        self.assertIsNone(r.write.clat_p99_ms)
        self.assertIsNone(r.write.clat_mean_ms)
        self.assertIsNone(r.write.iops)
        self.assertEqual(r.directions_with_io, {"read"})

    def test_bandwidth_is_mebibytes(self):
        r = parse_pod_json("pod-0", _fixture("ok_mixed.json"))
        self.assertAlmostEqual(r.read.bw_mibps, 122800000 / 1024.0 ** 2, places=3)

    def test_cpu_comes_from_the_json(self):
        """The v2 text parser only read the cpu line while no read/write
        section was open, and fio prints it after both, so it never matched
        and every CPU figure was zero."""
        r = parse_pod_json("pod-0", _fixture("ok_mixed.json"))
        self.assertAlmostEqual(r.usr_cpu, 1.33)
        self.assertAlmostEqual(r.sys_cpu, 6.89)
        self.assertEqual(r.ctx_switches, 32065615)

    def test_truncated_json_raises(self):
        with self.assertRaises(MissingMetric):
            parse_pod_json("pod-0", _fixture("truncated.json"))


class ValidateRunTest(unittest.TestCase):
    def _meta(self, **over):
        m = {"test_id": "t", "replicas": 1,
             "expected_directions": ["read", "write"],
             "runtime_s": 600, "rate_limited": True}
        m.update(over)
        return m

    def test_enospc_run_is_rejected(self):
        """results/test4_10pods_50k_5050_32kb: fio error 28, the write half
        produced nothing, and the old parser reported four PASS six WARN."""
        r = parse_pod_json("pod-0", _fixture("enospc_write_dead.json"))
        v = validate_run([r], self._meta())
        self.assertFalse(v.ok)
        joined = " ".join(v.failures)
        self.assertIn("fio error 28", joined)
        self.assertIn("ENOSPC", joined)
        self.assertIn("no I/O in expected direction 'write'", joined)

    def test_healthy_run_passes(self):
        r = parse_pod_json("pod-0", _fixture("ok_mixed.json"))
        v = validate_run([r], self._meta())
        self.assertTrue(v.ok, v.failures)

    def test_missing_pod_is_rejected(self):
        r = parse_pod_json("pod-0", _fixture("ok_mixed.json"))
        v = validate_run([r], self._meta(replicas=10))
        self.assertFalse(v.ok)
        self.assertIn("expected 10 pods, got 1", " ".join(v.failures))

    def test_read_only_meta_accepts_read_only_run(self):
        r = parse_pod_json("pod-0", _fixture("nsec_latency.json"))
        v = validate_run([r], self._meta(expected_directions=["read"],
                                         runtime_s=60))
        self.assertTrue(v.ok, v.failures)

    def test_io_without_percentiles_is_rejected(self):
        r = parse_pod_json("pod-0", _fixture("missing_percentiles.json"))
        v = validate_run([r], self._meta(expected_directions=["read"]))
        self.assertFalse(v.ok)
        self.assertIn("no clat percentiles", " ".join(v.failures))

    def test_short_run_is_rejected(self):
        r = parse_pod_json("pod-0", _fixture("nsec_latency.json"))
        v = validate_run([r], self._meta(expected_directions=["read"],
                                         runtime_s=600))
        self.assertFalse(v.ok)
        self.assertIn("ran 61s", " ".join(v.failures))

    def test_cpu_throttling_rejects_the_run(self):
        """Throttled CPU means the tail latency fio measured was the CFS
        scheduler, not the storage."""
        r = parse_pod_json("pod-0", _fixture("ok_mixed.json"))
        r.throttled_usec = 4_500_000
        v = validate_run([r], self._meta())
        self.assertFalse(v.ok)
        self.assertIn("throttled", " ".join(v.failures))


class CgroupTest(unittest.TestCase):
    LOG = (
        "some output\n"
        "===FIO_CGROUP_BEGIN===\n"
        "before: usage_usec 88171 nr_periods 10 nr_throttled 0 throttled_usec 0 \n"
        "after: usage_usec 900000 nr_periods 600 nr_throttled 12 throttled_usec 4500000 \n"
        "===FIO_CGROUP_END===\n"
    )

    def test_reads_throttling_delta(self):
        self.assertEqual(parse_cgroup_throttling(self.LOG), 4_500_000)

    def test_absent_block_is_none_not_zero(self):
        """None means 'not measured'; zero means 'measured, none happened'."""
        self.assertIsNone(parse_cgroup_throttling("no markers here"))

    def test_cgroup_v1_key(self):
        log = self.LOG.replace("throttled_usec", "throttled_time")
        self.assertEqual(parse_cgroup_throttling(log), 4_500_000)


if __name__ == "__main__":
    unittest.main()
