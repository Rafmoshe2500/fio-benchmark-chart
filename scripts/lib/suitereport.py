"""Summarise one suite run.

compare_envs.py answers "is A different from B". This answers the question
that comes first and more often: "what did this array actually do?" -- one
environment, every test, median and interval across repeats.

It is deliberately separate from the comparison, because a single run cannot
establish a difference and should not be laid out as though it could.
"""

from lib.envcompare import METRICS
from lib.stats import confidence_interval, median


def summarise_suite(suite):
    """Return {rows, warnings, missing, meta} for one loaded suite run."""
    warnings = []
    rows = []

    declared = list(suite.get("tests") or [])
    have = suite.get("results") or {}
    missing = [t for t in declared if t not in have]

    if suite.get("rejected"):
        warnings.append(
            "%d run(s) were rejected by the parser and are excluded; the "
            "figures below describe only the runs that completed as declared"
            % suite["rejected"])
        # A count on its own says a test produced nothing but not why, which
        # is the one thing needed to get it to run next time.
        for reason in (suite.get("rejections") or []):
            warnings.append("  %s" % reason)
        if not suite.get("rejections"):
            warnings.append(
                "  this suite predates rejection recording, so the reasons "
                "were printed to the terminal and not kept. A re-run records "
                "them in suite.json and keeps the deploy log.")

    if suite.get("repeats", 1) < 2:
        warnings.append(
            "this suite ran with %d repeat(s), so no interval can be given "
            "and nothing here is safe to quote as a comparison. Re-run with "
            "REPEATS=3 or more." % suite.get("repeats", 1))

    if suite.get("git_dirty"):
        warnings.append("run was made from a dirty working tree")

    for test_id in declared:
        vals = have.get(test_id)
        if not vals:
            continue
        for key, label, direction in METRICS:
            series = vals.get(key) or []
            if not series:
                continue
            med = median(series)
            lo, hi = confidence_interval(series)
            spread = None
            if lo is not None and med:
                spread = 100.0 * ((hi - lo) / 2.0) / med
            rows.append({
                "test_id": test_id,
                "metric": key,
                "label": label,
                "direction": direction,
                "n": len(series),
                "median": med,
                "min": min(series),
                "max": max(series),
                "ci95_lo": lo,
                "ci95_hi": hi,
                "spread_pct": spread,
            })

    return {"rows": rows, "warnings": warnings, "missing": missing,
            "meta": {k: suite.get(k) for k in
                     ("suite", "environment", "storage_class", "repeats",
                      "git_commit", "suite_id")}}


def _n(v, spec=",.1f"):
    return "n/a" if v is None else format(v, spec)


def print_suite_report(result):
    m = result["meta"]
    w = 96
    print("=" * w)
    print("  suite '%s' on '%s'  (%s)" % (m["suite"], m["environment"],
                                          m["storage_class"]))
    print("  %s repeat(s) per test   commit %s" % (m["repeats"], m["git_commit"]))
    print("=" * w)

    for msg in result["warnings"]:
        print("  warn  %s" % msg)
    if result["missing"]:
        print("  warn  no valid result for: %s" % ", ".join(result["missing"]))
    if result["warnings"] or result["missing"]:
        print()

    current = None
    for row in result["rows"]:
        if row["test_id"] != current:
            current = row["test_id"]
            print()
            print("  %s" % current)
            print("  " + "-" * (w - 4))
            print("    %-14s %12s %10s %10s   %s"
                  % ("metric", "median", "min", "max", "95% CI"))
        ci = ("[%s, %s]  +/-%.1f%%" % (_n(row["ci95_lo"]), _n(row["ci95_hi"]),
                                       row["spread_pct"])
              if row["ci95_lo"] is not None else "n=1, no interval")
        print("    %-14s %12s %10s %10s   %s" % (
            row["label"], _n(row["median"]), _n(row["min"]), _n(row["max"]), ci))

    print()
    print("=" * w)
    print("  Quote the median with its interval. To establish that another")
    print("  array differs, run the same suite there and use compare_envs.py.")
    print("=" * w)
