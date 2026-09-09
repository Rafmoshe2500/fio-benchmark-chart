import unittest

from lib.testselect import SelectionError, resolve_selection

# The real inventory, abbreviated to what matters for grouping.
AVAILABLE = [
    "test1_10pods_30k_5050_4kb",
    "test2_10pods_30k_5050_32kb",
    "test3_10pods_50k_5050_4kb",
    "test4_10pods_50k_5050_32kb",
    "test5_10pods_3gb_7030_256kb",
    "test6_10pods_3gb_7030_512kb",
    "test7_1pod_max_write_4kb",
    "test8_1pod_max_read_4kb",
    "test9_1pod_max_rw_7030_4kb",
    "test10_burst_write_phase1",
    "test10_burst_write_phase2",
    "test11_burst_read_phase1",
    "test11_burst_read_phase2",
    "test12_gradual_scale_32kb_7030",
    "test13_gradual_scale_512kb_7030",
    "test17_mixed_workload_32k",
    "test17_mixed_workload_64k",
    "test17_mixed_workload_256k",
    "test17_mixed_workload_512k",
    "low_qd_latency",
    "nifi_flowfile",
]


class RangesTest(unittest.TestCase):
    def test_the_example_from_the_request(self):
        """1-4,7-9,12,17 -- ranges, singles, and a group in one spec."""
        got = resolve_selection("1-4,7-9,12,17", AVAILABLE)
        self.assertEqual(got, [
            "test1_10pods_30k_5050_4kb",
            "test2_10pods_30k_5050_32kb",
            "test3_10pods_50k_5050_4kb",
            "test4_10pods_50k_5050_32kb",
            "test7_1pod_max_write_4kb",
            "test8_1pod_max_read_4kb",
            "test9_1pod_max_rw_7030_4kb",
            "test12_gradual_scale_32kb_7030",
            "test17_mixed_workload_256k",   # group entry point, see below
        ])

    def test_second_example(self):
        got = resolve_selection("5-8", AVAILABLE)
        self.assertEqual(got, [
            "test5_10pods_3gb_7030_256kb",
            "test6_10pods_3gb_7030_512kb",
            "test7_1pod_max_write_4kb",
            "test8_1pod_max_read_4kb",
        ])

    def test_ordering_follows_the_number_not_the_string(self):
        """test2 must come before test10, which sorting by name would not do."""
        got = resolve_selection("10,2", AVAILABLE)
        self.assertEqual(got[0], "test2_10pods_30k_5050_32kb")

    def test_whitespace_is_tolerated(self):
        self.assertEqual(resolve_selection(" 1 - 2 , 4 ", AVAILABLE),
                         resolve_selection("1-2,4", AVAILABLE))


class GroupCollapseTest(unittest.TestCase):
    def test_burst_pair_collapses_to_phase1(self):
        """deploy_test.sh deploys BOTH phases from the phase1 id. Listing
        phase2 as well would deploy the pair twice."""
        self.assertEqual(resolve_selection("10", AVAILABLE),
                         ["test10_burst_write_phase1"])

    def test_mixed_workload_collapses_to_one_entry(self):
        """test17 is four releases deployed from any one of its ids, for the
        same reason. The choice is alphabetical so it is stable rather than
        filesystem-order dependent; all four block sizes still run."""
        got = resolve_selection("17", AVAILABLE)
        self.assertEqual(len(got), 1)
        self.assertEqual(got, ["test17_mixed_workload_256k"])


class NamesTest(unittest.TestCase):
    def test_explicit_names_pass_through(self):
        self.assertEqual(resolve_selection("low_qd_latency,nifi_flowfile", AVAILABLE),
                         ["low_qd_latency", "nifi_flowfile"])

    def test_numbers_and_names_can_mix(self):
        got = resolve_selection("1,low_qd_latency", AVAILABLE)
        self.assertEqual(got, ["test1_10pods_30k_5050_4kb", "low_qd_latency"])

    def test_duplicates_are_removed_keeping_order(self):
        self.assertEqual(resolve_selection("1,1,2,1", AVAILABLE),
                         ["test1_10pods_30k_5050_4kb", "test2_10pods_30k_5050_32kb"])


class ErrorsTest(unittest.TestCase):
    def test_unknown_number_is_an_error(self):
        with self.assertRaises(SelectionError) as cm:
            resolve_selection("99", AVAILABLE)
        self.assertIn("99", str(cm.exception))

    def test_unknown_name_is_an_error(self):
        with self.assertRaises(SelectionError) as cm:
            resolve_selection("nope", AVAILABLE)
        self.assertIn("nope", str(cm.exception))

    def test_backwards_range_is_an_error(self):
        with self.assertRaises(SelectionError):
            resolve_selection("8-5", AVAILABLE)

    def test_empty_selection_is_an_error(self):
        with self.assertRaises(SelectionError):
            resolve_selection("  ", AVAILABLE)


if __name__ == "__main__":
    unittest.main()
