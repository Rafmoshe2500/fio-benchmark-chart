"""Load the declared intent of a test.

Profiles used to be guessed from a directory name. That classified 512K
random as sequential, read a 70/30 job as 50/50, and scored a 2,000 IOPS per
pod workload against a 15,000 IOPS target. Intent is declared, not inferred,
and a test without metadata is an error rather than a default.
"""

import json
import os

REQUIRED = ("test_id", "replicas", "numjobs",
            "expected_directions", "runtime_s", "rate_limited")
VALID_DIRECTIONS = {"read", "write"}


class MetaError(Exception):
    pass


def load_meta(test_id, meta_dir):
    path = os.path.join(meta_dir, "%s.meta.json" % test_id)
    if not os.path.exists(path):
        raise MetaError(
            "no metadata for '%s' at %s\n"
            "  Every test declares its targets in jobs/tests/<test_id>.meta.json.\n"
            "  Without it there is nothing to score the run against."
            % (test_id, path))
    try:
        with open(path) as fh:
            meta = json.load(fh)
    except ValueError as e:
        raise MetaError("%s: invalid JSON: %s" % (path, e))

    # Report every missing field at once: someone fixing this file should
    # not have to rerun the parser once per absent key.
    missing = [k for k in REQUIRED if k not in meta]
    if missing:
        raise MetaError("%s: missing required field(s): %s"
                        % (path, ", ".join(missing)))

    directions = meta["expected_directions"]
    if not directions:
        raise MetaError("%s: expected_directions must not be empty" % path)
    bad = set(directions) - VALID_DIRECTIONS
    if bad:
        raise MetaError("%s: unknown direction(s) %s; valid: %s"
                        % (path, sorted(bad), sorted(VALID_DIRECTIONS)))

    return meta
