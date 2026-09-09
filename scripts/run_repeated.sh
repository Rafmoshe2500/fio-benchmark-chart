#!/bin/bash
# Run a test several times and report the median with a confidence interval.
#
# A single run is not a measurement. Storage benchmarks are noisy, and an 8%
# gap between two StorageClasses means nothing until you know the spread
# within each one.
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

TEST_ID="${1:-}"
NAMESPACE="${2:-fio-tests}"
REPEATS="${REPEATS:-3}"
SETTLE="${SETTLE:-120}"

if [ -z "$TEST_ID" ]; then
  echo "Usage: ./run_repeated.sh <test_id> [namespace]"
  echo "  REPEATS  number of repetitions (default 3)"
  echo "  SETTLE   seconds between runs so the array is not still busy (default 120)"
  exit 1
fi

RUN_IDS=()
for r in $(seq 1 "$REPEATS"); do
  log "=== repetition $r of $REPEATS ==="
  "$CHART_DIR/scripts/deploy_test.sh" "$TEST_ID" "$NAMESPACE"
  RID="$(cat "$CHART_DIR/results/.last_run_id")"
  [ -n "$RID" ] || die "could not determine the run id for repetition $r"
  RUN_IDS+=("$RID")

  "$CHART_DIR/scripts/collect_results.sh" "$RID" "$NAMESPACE" || \
    warn "collection incomplete for repetition $r"
  ( cd "$CHART_DIR/scripts" && python3 parse_results.py "$CHART_DIR/results/$RID" ) || \
    warn "repetition $r was rejected and will be excluded from the aggregate"

  FORCE=true "$CHART_DIR/scripts/cleanup_test.sh" "$RID" "$NAMESPACE"

  # Let the array settle, otherwise repetition r+1 inherits r's cache state
  # and its numbers are not independent of it.
  if [ "$r" -lt "$REPEATS" ]; then
    log "settling ${SETTLE}s before the next repetition"
    sleep "$SETTLE"
  fi
done

echo
log "aggregating ${#RUN_IDS[@]} repetitions"

# argv[1] is the repo root: `python3 -` leaves __file__ undefined, so the
# path is passed in rather than derived.
( cd "$CHART_DIR/scripts" && python3 - "$CHART_DIR" "${RUN_IDS[@]}" ) <<'PY'
import json
import os
import sys

from lib.stats import confidence_interval, median, summarise

repo, run_ids = sys.argv[1], sys.argv[2:]

iops, bw, p99, kept = [], [], [], []
for rid in run_ids:
    path = os.path.join(repo, "results", rid, "summary_report.json")
    if not os.path.exists(path):
        print("  %-46s rejected, excluded" % rid)
        continue
    with open(path) as fh:
        d = json.load(fh)
    if not d.get("valid"):
        print("  %-46s invalid, excluded" % rid)
        continue
    agg = d["aggregate"]
    kept.append(rid)
    iops.append(agg["total_iops"])
    bw.append(agg["total_bw_mibps"])
    worst = [x for x in (agg.get("read_p99_worst_ms"), agg.get("write_p99_worst_ms"))
             if x is not None]
    p99.append(max(worst) if worst else None)
    print("  %-46s %12s IOPS  %10.1f MiB/s" % (
        rid, format(int(agg["total_iops"]), ","), agg["total_bw_mibps"]))

print()
if len(kept) < 2:
    sys.exit("Fewer than two valid repetitions. No interval can be reported, "
             "and a single run is not a result.")

for label, vals, unit in (("total IOPS", iops, ""),
                          ("bandwidth", bw, "MiB/s"),
                          ("worst P99", p99, "ms")):
    st = summarise(vals)
    if st["median"] is None:
        continue
    lo, hi = st["ci95_lo"], st["ci95_hi"]
    half = (hi - lo) / 2.0
    pct = 100.0 * half / st["median"] if st["median"] else 0.0
    print("%-11s n=%d  median %s %s   95%% CI [%s, %s]   +/-%.1f%%" % (
        label, st["n"], format(st["median"], ",.1f"), unit,
        format(lo, ",.1f"), format(hi, ",.1f"), pct))

print()
print("Quote the median with its interval. If two configurations' intervals")
print("overlap, the runs do not establish a difference between them -- see")
print("lib.stats.comparable().")
PY
