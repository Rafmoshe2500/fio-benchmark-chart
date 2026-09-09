"""Repeat-run statistics.

This module exists to stop a single run being quoted as a result. Storage
benchmarks are noisy; an 8% gap between two StorageClasses means nothing
until you know the spread within each one, and the honest answer to "which
is faster" is often "these runs cannot tell you".
"""

import math

# Two-sided 95% t values for small samples, indexed by degrees of freedom.
# Small n is the normal case here: nobody runs a 30-minute benchmark thirty
# times, so the normal approximation would understate the interval.
_T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571,
        6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228,
        11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131}


def median(values):
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    mid = len(v) // 2
    return v[mid] if len(v) % 2 else (v[mid - 1] + v[mid]) / 2.0


def confidence_interval(values, level=0.95):
    """95% CI of the mean via the t distribution.

    Returns (None, None) for a single sample: one measurement has no spread,
    and reporting a zero-width interval would imply a precision the run
    cannot support.
    """
    v = [x for x in values if x is not None]
    n = len(v)
    if n < 2:
        return (None, None)
    mean = sum(v) / n
    var = sum((x - mean) ** 2 for x in v) / (n - 1)
    se = math.sqrt(var / n)
    t = _T95.get(n - 1, 1.96)
    return (mean - t * se, mean + t * se)


def comparable(a, b):
    """True when the two sets' 95% intervals do not overlap.

    False means "do not claim a difference" -- either because the difference
    is inside the noise, or because there are too few runs to tell.
    """
    a_lo, a_hi = confidence_interval(a)
    b_lo, b_hi = confidence_interval(b)
    if a_lo is None or b_lo is None:
        return False
    return a_hi < b_lo or b_hi < a_lo


def relative_change(value, baseline):
    """Percentage change against a baseline, or None when there is none."""
    if not baseline:
        return None
    return 100.0 * (value - baseline) / baseline


def summarise(values):
    """median, 95% CI and spread, as a dict. None-safe throughout."""
    v = [x for x in values if x is not None]
    lo, hi = confidence_interval(v)
    return {
        "n": len(v),
        "median": median(v),
        "mean": (sum(v) / len(v)) if v else None,
        "min": min(v) if v else None,
        "max": max(v) if v else None,
        "ci95_lo": lo,
        "ci95_hi": hi,
    }
