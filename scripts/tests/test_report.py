import unittest

from lib.fiojson import DirectionResult, PodResult
from lib.report import aggregate, cluster_percentile, merged_percentile


def _pod(name, r_iops, r_p99, w_iops, w_p99):
    return PodResult(
        pod=name, error=0, elapsed_s=600,
        read=DirectionResult("read", iops=r_iops, io_bytes=1, runtime_ms=600000,
                             bw_mibps=r_iops * 0.004, clat_p99_ms=r_p99),
        write=DirectionResult("write", iops=w_iops, io_bytes=1, runtime_ms=600000,
                              bw_mibps=w_iops * 0.004, clat_p99_ms=w_p99),
    )


class ClusterPercentileTest(unittest.TestCase):
    def test_reports_worst_pod_not_the_mean(self):
        """One pod at 400ms among nine at 5ms is the number that matters.
        The mean, 44.5ms, hides the only pod anyone cares about."""
        pods = [_pod("p%d" % i, 1000, 5.0, 1000, 5.0) for i in range(9)]
        pods.append(_pod("p9", 1000, 400.0, 1000, 400.0))
        self.assertEqual(cluster_percentile(pods, "read", "clat_p99_ms"), 400.0)

    def test_ignores_pods_without_the_metric(self):
        pods = [_pod("p0", 1000, 5.0, 1000, 5.0),
                PodResult(pod="p1", read=DirectionResult("read"),
                          write=DirectionResult("write"))]
        self.assertEqual(cluster_percentile(pods, "read", "clat_p99_ms"), 5.0)

    def test_none_when_no_pod_has_it(self):
        pods = [PodResult(pod="p0", read=DirectionResult("read"),
                          write=DirectionResult("write"))]
        self.assertIsNone(cluster_percentile(pods, "read", "clat_p99_ms"))


class MergedPercentileTest(unittest.TestCase):
    def test_merges_histogram_bins_across_pods(self):
        """json+ emits clat_ns.bins. Summing counts across pods and walking
        to the qth sample is the real distribution, not an approximation."""
        # 99 samples at 1us, 1 sample at 1s, across two pods
        a = {"1000": 50, "1000000000": 0}
        b = {"1000": 49, "1000000000": 1}
        self.assertAlmostEqual(merged_percentile([a, b], 0.50), 0.001)
        self.assertAlmostEqual(merged_percentile([a, b], 0.999), 1000.0)

    def test_none_on_empty(self):
        self.assertIsNone(merged_percentile([], 0.99))
        self.assertIsNone(merged_percentile([{}, None], 0.99))


class AggregateTest(unittest.TestCase):
    def test_iops_sum_and_target_attainment(self):
        pods = [_pod("p%d" % i, 1500, 5.0, 1500, 5.0) for i in range(10)]
        meta = {"replicas": 10, "target_iops_total": 30000,
                "expected_directions": ["read", "write"]}
        agg = aggregate(pods, meta)
        self.assertEqual(agg["read_iops_total"], 15000)
        self.assertEqual(agg["write_iops_total"], 15000)
        self.assertAlmostEqual(agg["iops_attainment_pct"], 100.0)

    def test_no_attainment_without_a_declared_target(self):
        """A ceiling test has no target by design. Inventing one is how the
        old parser graded a run against a profile it never claimed."""
        pods = [_pod("p0", 1500, 5.0, 1500, 5.0)]
        agg = aggregate(pods, {"replicas": 1,
                               "expected_directions": ["read", "write"]})
        self.assertIsNone(agg["iops_attainment_pct"])
        self.assertIsNone(agg["bw_attainment_pct"])

    def test_worst_p99_lands_in_the_aggregate(self):
        pods = [_pod("p0", 1000, 5.0, 1000, 5.0),
                _pod("p1", 1000, 90.0, 1000, 7.0)]
        agg = aggregate(pods, {"replicas": 2,
                               "expected_directions": ["read", "write"]})
        self.assertEqual(agg["read_p99_worst_ms"], 90.0)
        self.assertEqual(agg["write_p99_worst_ms"], 7.0)


if __name__ == "__main__":
    unittest.main()
