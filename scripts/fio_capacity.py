#!/usr/bin/env python3
"""Print the PVC size a fio job file actually needs.

    ./fio_capacity.py jobs/tests/test4_10pods_50k_5050_32kb.fio
    96

With --pvc, exit 1 and explain when the proposed PVC is too small. This is
the check that would have stopped the test4 ENOSPC run before it started.
"""

import argparse
import math
import sys

from lib.fiojob import parse_job_file, parse_size


def main():
    p = argparse.ArgumentParser()
    p.add_argument("job_file")
    p.add_argument("--headroom", type=float, default=1.2,
                   help="multiplier over size*numjobs (default 1.2)")
    p.add_argument("--pvc", default="",
                   help="proposed PVC size, e.g. 96Gi; exits 1 if too small")
    a = p.parse_args()

    spec = parse_job_file(a.job_file)
    if spec.size_bytes == 0:
        sys.exit("%s: no size= in [global]; cannot size a PVC" % a.job_file)

    rounded = int(math.ceil(spec.required_gib(a.headroom)))

    if not a.pvc:
        print(rounded)
        return 0

    have = parse_size(a.pvc[:-1] if a.pvc.lower().endswith("i") else a.pvc)
    if have < spec.required_bytes * a.headroom:
        sys.exit(
            "%s: PVC %s is too small.\n"
            "  size=%dGiB x numjobs=%d = %dGiB of dataset\n"
            "  with %d%% headroom that is %dGi\n"
            "  fio will fail with ENOSPC partway through the run." % (
                a.job_file, a.pvc,
                spec.size_bytes / 1024 ** 3, spec.numjobs,
                spec.required_bytes / 1024 ** 3,
                int(round((a.headroom - 1) * 100)), rounded))
    print(rounded)
    return 0


if __name__ == "__main__":
    sys.exit(main())
