"""Compare the same suite run against different environments.

The question this answers is the one the whole project exists for: "is array
A faster than array B for our workload?" -- and, just as often, "these runs
do not establish that either way", which is a real answer and the one most
easily skipped.

Two guards. A comparison of different suites is refused outright, because the
tests are not the same question. A comparison whose intervals overlap is
reported as no difference established, however large the gap between the
medians looks.
"""

import glob
import json
import os

from lib.stats import comparable, confidence_interval, median, relative_change

METRICS = (
    ("total_iops", "IOPS", "higher"),
    ("total_bw_mibps", "MiB/s", "higher"),
    ("read_p99_worst_ms", "read P99 ms", "lower"),
    ("write_p99_worst_ms", "write P99 ms", "lower"),
)


class CompareError(Exception):
    pass


def load_suite(path):
    """Read one suite run directory into {meta..., results: {test: {metric: [..]}}}."""
    sj = os.path.join(path, "suite.json")
    if not os.path.exists(sj):
        raise CompareError(
            "%s is not a suite run directory (no suite.json).\n"
            "  Suite runs are produced by ./scripts/run_suite.sh and live\n"
            "  under results/suites/." % path)
    with open(sj) as fh:
        suite = json.load(fh)

    results = {}
    for test_id in suite.get("tests", []):
        reps = sorted(glob.glob(os.path.join(path, test_id, "rep-*.json")))
        values = {k: [] for k, _, _ in METRICS}
        for r in reps:
            try:
                with open(r) as fh:
                    d = json.load(fh)
            except (ValueError, OSError):
                continue
            # A rejected run wrote no summary, but guard anyway.
            if not d.get("valid"):
                continue
            agg = d.get("aggregate") or {}
            for key, _, _ in METRICS:
                v = agg.get(key)
                if v is not None:
                    values[key].append(float(v))
        if any(values[k] for k, _, _ in METRICS):
            results[test_id] = values

    suite["results"] = results
    suite["path"] = path
    return suite


def compare_suites(suites):
    """Compare two or more loaded suites. Returns {rows, warnings, baseline}."""
    if len(suites) < 2:
        raise CompareError("need at least two suite runs to compare")

    names = {s["suite"] for s in suites}
    if len(names) > 1:
        raise CompareError(
            "refusing to compare different suites: %s.\n"
            "  The tests are not the same question. Re-run the same suite\n"
            "  against each environment." % ", ".join(sorted(names)))

    warnings = []

    commits = {s.get("git_commit", "?") for s in suites}
    if len(commits) > 1:
        warnings.append(
            "runs are from different git commits (%s); the job files or the "
            "harness may differ between them" % ", ".join(sorted(commits)))

    if any(s.get("repeats", 1) < 2 for s in suites):
        warnings.append(
            "at least one environment ran with fewer than 2 repeats, so no "
            "difference can be established from these runs however large the "
            "gap looks. Re-run with REPEATS=3 or more.")

    if any(s.get("git_dirty") for s in suites):
        warnings.append("at least one run was made from a dirty working tree")

    # Tests present in one environment but not another.
    all_tests = sorted({t for s in suites for t in s["results"]})
    for t in all_tests:
        missing = [s["environment"] for s in suites if t not in s["results"]]
        if missing:
            warnings.append("%s has no valid result in: %s" % (t, ", ".join(missing)))

    baseline = suites[0]
    rows = []
    for test_id in all_tests:
        for key, label, direction in METRICS:
            series = {}
            for s in suites:
                vals = (s["results"].get(test_id) or {}).get(key) or []
                if vals:
                    series[s["environment"]] = vals
            if len(series) < 2:
                continue

            meds = {e: median(v) for e, v in series.items()}
            base_env = baseline["environment"]
            base = meds.get(base_env)

            best_env = (max(meds, key=lambda e: meds[e]) if direction == "higher"
                        else min(meds, key=lambda e: meds[e]))

            # A difference counts only when it survives the noise, and only
            # against the baseline it is being reported relative to.
            established = any(
                comparable(series[base_env], series[e])
                for e in series if e != base_env) if base_env in series else False

            if not established:
                verdict = "no difference established"
            else:
                verdict = "%s %s" % (best_env, "higher" if direction == "higher" else "lower")

            rows.append({
                "test_id": test_id,
                "metric": key,
                "label": label,
                "direction": direction,
                "medians": meds,
                "intervals": {e: confidence_interval(v) for e, v in series.items()},
                "n": {e: len(v) for e, v in series.items()},
                "relative_to_baseline": {
                    e: relative_change(meds[e], base) for e in meds
                } if base else {},
                "established": established,
                "verdict": verdict,
            })

    return {"rows": rows, "warnings": warnings,
            "baseline": baseline["environment"],
            "environments": [s["environment"] for s in suites],
            "suite": suites[0]["suite"]}


def _fmt(v, spec=",.1f"):
    return "n/a" if v is None else format(v, spec)


def print_comparison(result):
    envs = result["environments"]
    w = 100
    print("=" * w)
    print("  suite '%s' across %d environment(s): %s"
          % (result["suite"], len(envs), ", ".join(envs)))
    print("  baseline: %s" % result["baseline"])
    print("=" * w)

    for msg in result["warnings"]:
        print("  warn  %s" % msg)
    if result["warnings"]:
        print()

    current = None
    for row in result["rows"]:
        if row["test_id"] != current:
            current = row["test_id"]
            print()
            print("  %s" % current)
            print("  " + "-" * (w - 2))
        meds = " ".join("%s=%s" % (e, _fmt(row["medians"].get(e)))
                        for e in envs if e in row["medians"])
        rel = ""
        base = result["baseline"]
        others = [e for e in envs if e != base and row["relative_to_baseline"].get(e) is not None]
        if others:
            rel = "  (" + ", ".join(
                "%s %+.1f%%" % (e, row["relative_to_baseline"][e]) for e in others) + ")"
        mark = "*" if row["established"] else " "
        print("  %s %-14s %-46s%s" % (mark, row["label"], meds, rel))
        if not row["established"]:
            print("      %s -- intervals overlap" % row["verdict"])
        else:
            print("      %s" % row["verdict"])

    print()
    print("=" * w)
    print("  * marks a difference whose 95% intervals do not overlap.")
    print("  Rows without it are not evidence of a difference, regardless of")
    print("  how far apart the medians are.")
    print("=" * w)
