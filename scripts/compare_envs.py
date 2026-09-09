#!/usr/bin/env python3
"""Compare the same suite run against different environments.

    ./compare_envs.py results/suites/quick-nfs3-...  results/suites/quick-nfs41-...

The first directory is the baseline; percentages are relative to it.

Exit codes:
    0  comparison printed
    2  the runs are not comparable
"""

import argparse
import json
import sys

from lib.envcompare import CompareError, compare_suites, load_suite, print_comparison


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("suite_dirs", nargs="+",
                    help="suite run directories; the first is the baseline")
    ap.add_argument("--json", default="", help="also write the result here")
    a = ap.parse_args()

    try:
        suites = [load_suite(d) for d in a.suite_dirs]
        result = compare_suites(suites)
    except CompareError as e:
        sys.stderr.write(str(e) + "\n")
        return 2

    print_comparison(result)

    if a.json:
        with open(a.json, "w") as fh:
            json.dump(result, fh, indent=2, default=str)
        print("\nwrote %s" % a.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
