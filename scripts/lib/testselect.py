"""Turn an ad-hoc test selection into a list of test ids.

    resolve_selection("1-4,7-9,12,17", available)

Accepts test numbers, ranges of numbers, explicit test ids, and any mix.
Numbers exist because that is how the tests are actually referred to in
conversation ("run 5 through 8"), and translating that by hand into
test5_10pods_3gb_7030_256kb every time is where mistakes get made.

Two of the numbers do not map to a single job file, and this module collapses
them the way deploy_test.sh expects:

  10, 11  are two-phase burst tests. deploy_test.sh deploys BOTH phases from
          the phase1 id, so only phase1 belongs in a selection. Listing
          phase2 as well would deploy the pair twice.
  17      is four block-size releases deployed from any one of its ids, for
          the same reason.
"""

import re

_TESTNUM = re.compile(r"^test(\d+)_")


class SelectionError(Exception):
    pass


def _by_number(available):
    """{number: [test_id, ...]} in file order."""
    out = {}
    for t in available:
        m = _TESTNUM.match(t)
        if m:
            out.setdefault(int(m.group(1)), []).append(t)
    return out


def _collapse(number, ids):
    """One deployable entry per test number.

    deploy_test.sh matches on a substring and expands groups itself, so a
    selection must name the group once, not once per member.
    """
    if len(ids) == 1:
        return ids[0]
    phase1 = [t for t in ids if t.endswith("_phase1")]
    if phase1:
        return phase1[0]
    # test17-style: several releases, any one id triggers all of them.
    # Sort so the choice is stable rather than filesystem-order dependent.
    return sorted(ids)[0]


def resolve_selection(spec, available):
    """Return an ordered, de-duplicated list of test ids."""
    if not spec or not spec.strip():
        raise SelectionError("empty selection")

    by_num = _by_number(available)
    have = set(available)
    picked, seen = [], set()

    def add(test_id):
        if test_id not in seen:
            seen.add(test_id)
            picked.append(test_id)

    def add_number(n, token):
        if n not in by_num:
            raise SelectionError(
                "no test numbered %d (from %r). Known numbers: %s"
                % (n, token, ", ".join(str(k) for k in sorted(by_num))))
        add(_collapse(n, by_num[n]))

    # Numbers are ordered numerically, not lexically, so 2 precedes 10.
    numeric, named = [], []
    for raw in spec.split(","):
        token = raw.strip()
        if not token:
            continue
        rng = re.match(r"^(\d+)\s*-\s*(\d+)$", token)
        if rng:
            lo, hi = int(rng.group(1)), int(rng.group(2))
            if lo > hi:
                raise SelectionError("range %r runs backwards" % token)
            numeric.extend((n, token) for n in range(lo, hi + 1))
        elif token.isdigit():
            numeric.append((int(token), token))
        elif token in have:
            named.append(token)
        else:
            raise SelectionError(
                "unknown test %r. Use a test number, a range like 5-8, or a "
                "full test id." % token)

    for n, token in sorted(numeric, key=lambda p: p[0]):
        add_number(n, token)
    for t in named:
        add(t)

    if not picked:
        raise SelectionError("selection %r matched no tests" % spec)
    return picked
