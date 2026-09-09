#!/usr/bin/env python3
"""Parse a collected fio benchmark run and report it -- or refuse to.

    ./parse_results.py results/test1-10pods-...-20260909-120000

Exit codes:
    0  run valid, report printed, summary files written
    2  run rejected: incomplete, errored, or missing required metrics
    3  could not load the run at all
"""

import argparse
import glob
import json
import os
import sys

from lib.fiojson import (MissingMetric, parse_cgroup_throttling,
                         parse_pod_json, validate_run)
from lib.report import aggregate, print_report, write_csv, write_json
from lib.testmeta import MetaError, load_meta

JSON_BEGIN = "===FIO_JSON_BEGIN==="
JSON_END = "===FIO_JSON_END==="


def extract_json(log_text, pod):
    """Pull the json+ block out of a pod log.

    The human-readable fio output is still in the log above it; we
    deliberately do not parse that. Text parsing is what produced the
    nsec/usec confusion and the CPU line that was never matched.
    """
    if JSON_BEGIN not in log_text:
        raise MissingMetric(
            "%s: no %s marker in the log.\n"
            "  The pod ran without --output-format=json+, or was collected\n"
            "  before it finished. Check fioOutput.json in values.yaml."
            % (pod, JSON_BEGIN))
    body = log_text.split(JSON_BEGIN, 1)[1]
    if JSON_END not in body:
        raise MissingMetric("%s: log truncated between the JSON markers" % pod)
    return body.split(JSON_END, 1)[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("results_dir")
    ap.add_argument("--meta-dir", default=None,
                    help="defaults to <repo>/jobs/tests")
    a = ap.parse_args()

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # A run is only reportable when we know what was deployed. Without the
    # manifest a directory of logs has no declared intent to check against.
    # Step subdirectories inherit their parent's manifest.
    manifest_path = os.path.join(a.results_dir, "manifest.json")
    if not os.path.exists(manifest_path):
        manifest_path = os.path.join(
            os.path.dirname(os.path.normpath(a.results_dir)), "manifest.json")
    if not os.path.exists(manifest_path):
        sys.stderr.write(
            "%s: no manifest.json.\n"
            "  Runs are reportable only when deploy_test.sh recorded what it\n"
            "  deployed. Re-run through ./scripts/deploy_test.sh.\n"
            % a.results_dir)
        return 3
    with open(manifest_path) as fh:
        manifest = json.load(fh)

    # jobs/tests is the scenario suite, jobs/profiles the workload matrix.
    meta_dir = a.meta_dir
    if not meta_dir:
        for d in ("tests", "profiles"):
            cand = os.path.join(repo, "jobs", d)
            if os.path.exists(os.path.join(
                    cand, "%s.meta.json" % manifest["test_id"])):
                meta_dir = cand
                break
        meta_dir = meta_dir or os.path.join(repo, "jobs", "tests")

    try:
        meta = load_meta(manifest["test_id"], meta_dir)
    except MetaError as e:
        sys.stderr.write(str(e) + "\n")
        return 3

    logs = sorted(glob.glob(os.path.join(a.results_dir, "*.log")))
    if not logs:
        sys.stderr.write("no .log files in %s\n" % a.results_dir)
        return 3

    pods, load_failures = [], []
    for path in logs:
        pod = os.path.basename(path)[:-4]
        with open(path, errors="replace") as fh:
            text = fh.read()
        try:
            result = parse_pod_json(pod, extract_json(text, pod))
        except MissingMetric as e:
            load_failures.append(str(e))
            continue
        result.throttled_usec = parse_cgroup_throttling(text)
        pods.append(result)

    validation = validate_run(pods, meta)
    for f in load_failures:
        validation.fail(f)

    agg = aggregate(pods, meta) if pods else {}
    print_report(pods, meta, agg, validation)

    if validation.ok:
        write_csv(os.path.join(a.results_dir, "summary_report.csv"), pods)
        write_json(os.path.join(a.results_dir, "summary_report.json"),
                   meta, agg, validation, pods)
        print("\nwrote summary_report.csv and summary_report.json to %s"
              % a.results_dir)
        return 0

    print("\nNo summary was written. Fix the run; do not report these numbers.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
