#!/usr/bin/env python3
"""Check every test's declared intent against what will actually be deployed.

    ./scripts/validate_tests.py                 check everything
    ./scripts/validate_tests.py low_qd_latency max_throughput

Exit 0 when every test is consistent, 1 otherwise.

This exists because of a specific failure: the `characterise` suite ran for
hours and every single test was rejected at the end with "expected 10 pods,
got 4". The pod count was declared twice -- once in each profile's
.meta.json, once as a hardcoded default in deploy_test.sh's case statement --
and the two had drifted for all thirteen profiles. Nothing checked, so the
first sign was the rejection after the measurement was already spent.

Every check here answers one question: would parse_results.py reject a
perfectly healthy run of this test because the declaration and the job file
disagree? Those are the failures that cost hours and produce nothing, and
they are all knowable in advance from files on disk.
"""

import argparse
import glob
import io
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib.fiojob import parse_job_file, parse_size
from lib.testmeta import MetaError, load_meta

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOB_DIRS = ("tests", "profiles")


def discover():
    """All test ids that have a job file, with the directory holding them."""
    found = {}
    for d in JOB_DIRS:
        for f in sorted(glob.glob(os.path.join(REPO, "jobs", d, "*.fio"))):
            found[os.path.basename(f)[:-4]] = os.path.join(REPO, "jobs", d)
    return found


def pvc_sizes():
    sizes = {}
    path = os.path.join(REPO, "scripts", "pvc_sizes.conf")
    with io.open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) >= 2:
                sizes[parts[0]] = parts[1]
    return sizes


def _int_opt(spec, key):
    v = spec.option(key)
    if v is None:
        return None
    try:
        return int(str(v).strip().rstrip("s"))
    except ValueError:
        return None


def effective_directions(spec):
    """What the job file can actually produce.

    fiojob reports randrw as both directions regardless of the mix, which is
    right for capacity but wrong here: rwmixread=100 produces no writes at
    all, and a meta that expects one would reject every healthy run.
    """
    dirs = set(spec.directions)
    if dirs == {"read", "write"}:
        mix = _int_opt(spec, "rwmixread")
        if mix == 100:
            return {"read"}
        if mix == 0:
            return {"write"}
    return dirs


def check(test_id, job_dir, sizes):
    """Return a list of problem strings. Empty means consistent."""
    problems = []
    job_path = os.path.join(job_dir, test_id + ".fio")

    try:
        meta = load_meta(test_id, job_dir)
    except MetaError as e:
        return [str(e)]

    spec = parse_job_file(job_path)

    if meta.get("test_id") != test_id:
        problems.append(
            "meta test_id is %r but the file is named %r; parse_results.py "
            "looks the metadata up by file name"
            % (meta.get("test_id"), test_id))

    replicas = meta.get("replicas")
    if not isinstance(replicas, int) or replicas < 1:
        problems.append("replicas is %r; must be a positive integer" % replicas)

    # numjobs: meta counts total clones, which is what fio's group_reporting
    # collapses into one entry and what the capacity check multiplies size by.
    if spec.numjobs != meta.get("numjobs"):
        problems.append(
            "numjobs: the job file yields %d clone(s) (numjobs x %d section(s)) "
            "but the metadata declares %s"
            % (spec.numjobs, max(len(spec.sections), 1), meta.get("numjobs")))

    runtime = _int_opt(spec, "runtime")
    if runtime is not None and runtime != meta.get("runtime_s"):
        problems.append(
            "runtime: the job file runs %ds but the metadata declares %ss; "
            "parse_results.py rejects a run shorter than 90%% of the declared "
            "runtime" % (runtime, meta.get("runtime_s")))

    ramp = _int_opt(spec, "ramp_time")
    if ramp is not None and meta.get("ramp_s") is not None \
            and ramp != meta["ramp_s"]:
        problems.append("ramp_time: job file %ds, metadata %ss"
                        % (ramp, meta["ramp_s"]))

    can = effective_directions(spec)
    want = set(meta.get("expected_directions") or [])
    impossible = sorted(want - can)
    if impossible and can:
        problems.append(
            "expected_directions %s, but the job file can only produce %s; "
            "every healthy run would be rejected for 'no I/O in expected "
            "direction'" % (sorted(want), sorted(can)))

    # PVC: the ENOSPC class. deploy_test.sh already refuses at deploy time,
    # but a suite should learn about it before the first test, not the fifth.
    size = sizes.get(test_id)
    if size is None:
        problems.append(
            "no PVC size registered in scripts/pvc_sizes.conf; "
            "regenerate it with ./scripts/gen_pvc_sizes.sh")
    elif spec.size_bytes == 0:
        problems.append("no size= in [global]; the PVC cannot be sized")
    else:
        raw = size[:-1] if size.lower().endswith("i") else size
        have = parse_size(raw)
        need = spec.required_bytes * 1.2
        if have < need:
            problems.append(
                "PVC %s is too small: size=%dGiB x numjobs=%d needs %dGi with "
                "headroom; fio would fail with ENOSPC partway through"
                % (size, spec.size_bytes / 1024 ** 3, spec.numjobs,
                   int(math.ceil(need / 1024 ** 3))))

    return problems


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("tests", nargs="*",
                   help="test ids to check (default: every test)")
    p.add_argument("--quiet", action="store_true",
                   help="print nothing when everything is consistent")
    a = p.parse_args(argv)

    found = discover()
    wanted = a.tests or sorted(found)
    sizes = pvc_sizes()

    unknown = [t for t in wanted if t not in found]
    bad = 0
    for t in unknown:
        print("FAIL %s: no such job file in jobs/tests or jobs/profiles" % t)
        bad += 1

    for t in [t for t in wanted if t in found]:
        problems = check(t, found[t], sizes)
        if problems:
            bad += 1
            print("FAIL %s" % t)
            for msg in problems:
                for i, line in enumerate(msg.splitlines()):
                    print("       %s" % line if i else "     - %s" % line)
        elif not a.quiet:
            print("ok   %s" % t)

    checked = len(wanted)
    if bad:
        print()
        print("%d of %d test(s) would be rejected after running. Fix the "
              "declaration or the job file before spending the run time."
              % (bad, checked))
        return 1
    if not a.quiet:
        print()
        print("%d test(s) consistent" % checked)
    return 0


if __name__ == "__main__":
    sys.exit(main())
