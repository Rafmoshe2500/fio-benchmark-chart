import unittest

from nififlow import _pct, _slope, cluster_series


def _samples(node_rates, start=1000, step=10):
    """Build sample rows whose counter_bytes deltas produce `node_rates`."""
    rows = {}
    for node, rates in node_rates.items():
        total, s = 0, [{"ts": start, "counter_bytes": 0, "queued_count": 0}]
        for i, r in enumerate(rates, start=1):
            total += int(r * step)
            s.append({"ts": start + i * step, "counter_bytes": total,
                      "queued_count": 0})
        rows[node] = s
    return rows


class ClusterSeriesTest(unittest.TestCase):
    def test_sums_across_nodes_within_a_bucket(self):
        """Two nodes at 100 B/s each is a cluster doing 200 B/s, at every
        point in time -- not two separate 100s."""
        rows = _samples({"a": [100.0, 100.0], "b": [100.0, 100.0]})
        self.assertEqual(sorted(cluster_series(rows)), [200.0, 200.0])

    def test_does_not_scale_a_per_node_percentile_by_node_count(self):
        """The old code did _pct(rates,.95) * len(rows), which assumes every
        node peaks in the same second. Here node a peaks in the first bucket
        and node b in the second, so the cluster never exceeds 1100 B/s --
        the old formula would have claimed 2000."""
        rows = _samples({"a": [1000.0, 100.0], "b": [100.0, 1000.0]})
        series = cluster_series(rows)
        self.assertEqual(sorted(series), [1100.0, 1100.0])
        self.assertEqual(_pct(series, 0.95), 1100.0)

    def test_ignores_non_advancing_timestamps(self):
        rows = {"a": [{"ts": 1000, "counter_bytes": 0, "queued_count": 0},
                      {"ts": 1000, "counter_bytes": 500, "queued_count": 0}]}
        self.assertEqual(cluster_series(rows), [])

    def test_counter_resets_do_not_produce_negative_rates(self):
        """A counter reset mid-run would otherwise show as a large negative
        rate and drag the cluster series below zero."""
        rows = {"a": [{"ts": 1000, "counter_bytes": 5000, "queued_count": 0},
                      {"ts": 1010, "counter_bytes": 0, "queued_count": 0}]}
        self.assertEqual(cluster_series(rows), [0.0])

    def test_empty_input(self):
        self.assertEqual(cluster_series({}), [])


class PctTest(unittest.TestCase):
    def test_percentiles(self):
        """Nearest-rank on a 0-indexed sorted list: index round(q*(n-1))."""
        v = list(range(1, 101))
        self.assertEqual(_pct(v, 0.5), 51)
        self.assertEqual(_pct(v, 0.95), 95)
        self.assertEqual(_pct(v, 1.0), 100)

    def test_sorts_its_input(self):
        self.assertEqual(_pct([9, 1, 5], 0.0), 1)
        self.assertEqual(_pct([9, 1, 5], 1.0), 9)

    def test_empty_is_zero(self):
        self.assertEqual(_pct([], 0.95), 0)


class SlopeTest(unittest.TestCase):
    def test_rising_queue_has_positive_slope(self):
        self.assertAlmostEqual(_slope([0, 10, 20, 30], [0, 100, 200, 300]), 10.0)

    def test_flat_queue_has_zero_slope(self):
        self.assertAlmostEqual(_slope([0, 10, 20], [5, 5, 5]), 0.0)

    def test_single_sample_is_zero(self):
        self.assertEqual(_slope([0], [5]), 0.0)


if __name__ == "__main__":
    unittest.main()
