import unittest

from lib.stats import comparable, confidence_interval, median, relative_change


class MedianTest(unittest.TestCase):
    def test_odd_and_even(self):
        self.assertEqual(median([3, 1, 2]), 2)
        self.assertEqual(median([4, 1, 3, 2]), 2.5)

    def test_empty_is_none(self):
        self.assertIsNone(median([]))

    def test_ignores_none_entries(self):
        self.assertEqual(median([1, None, 3]), 2)


class ConfidenceIntervalTest(unittest.TestCase):
    def test_three_samples_bracket_the_mean(self):
        lo, hi = confidence_interval([100.0, 102.0, 98.0])
        self.assertLess(lo, 100.0)
        self.assertGreater(hi, 100.0)

    def test_single_sample_has_no_interval(self):
        """One measurement has no spread. Reporting +/- 0 would imply a
        precision that a single run cannot support."""
        self.assertEqual(confidence_interval([100.0]), (None, None))

    def test_identical_samples_give_a_zero_width_interval(self):
        lo, hi = confidence_interval([50.0, 50.0, 50.0])
        self.assertAlmostEqual(lo, 50.0)
        self.assertAlmostEqual(hi, 50.0)

    def test_noisier_data_gives_a_wider_interval(self):
        tight = confidence_interval([100.0, 101.0, 99.0])
        loose = confidence_interval([100.0, 140.0, 60.0])
        self.assertGreater(loose[1] - loose[0], tight[1] - tight[0])


class ComparableTest(unittest.TestCase):
    def test_overlapping_intervals_are_not_a_difference(self):
        """The case this module exists for: two storage classes 3% apart with
        run-to-run noise larger than 3%. That is not a difference."""
        self.assertFalse(comparable([100.0, 101.0, 99.0], [103.0, 104.0, 102.0]))

    def test_separated_intervals_are_a_difference(self):
        self.assertTrue(comparable([100.0, 101.0, 99.0], [200.0, 201.0, 199.0]))

    def test_a_single_run_can_never_establish_a_difference(self):
        self.assertFalse(comparable([100.0], [200.0]))


class RelativeChangeTest(unittest.TestCase):
    def test_percentage_against_a_baseline(self):
        self.assertAlmostEqual(relative_change(120.0, 100.0), 20.0)
        self.assertAlmostEqual(relative_change(80.0, 100.0), -20.0)

    def test_zero_baseline_is_none(self):
        self.assertIsNone(relative_change(50.0, 0.0))


if __name__ == "__main__":
    unittest.main()
