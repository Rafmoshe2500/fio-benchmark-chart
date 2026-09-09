#!/usr/bin/env python3
"""Summarise one suite run -- what this array actually did.

    ./suite_report.py results/suites/ceiling-nfs3-20260909-201500

For "is this array different from that one", run the same suite on both and
use compare_envs.py.
"""

import argparse
import json
import sys

from lib.envcompare import CompareError, load_suite
from lib.suitereport import print_suite_report, summarise_suite


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("suite_dir")
    ap.add_argument("--json", default="", help="also write the result here")
    a = ap.parse_args()

    try:
        result = summarise_suite(load_suite(a.suite_dir))
    except CompareError as e:
        sys.stderr.write(str(e) + "\n")
        return 2

    print_suite_report(result)
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(result, fh, indent=2, default=str)
        print("\nwrote %s" % a.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
