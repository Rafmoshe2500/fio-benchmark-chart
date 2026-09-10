#!/bin/bash
# Run a named suite of tests against one named environment.
#
#   ./scripts/run_suite.sh <suite|selection> <environment> [namespace]
#
#   ./scripts/run_suite.sh characterise nfs3        a named suite
#   ./scripts/run_suite.sh manual nfs3              the suite you edit
#   ./scripts/run_suite.sh 1-4,7-9,12,17 nfs3       an ad-hoc selection
#   REPEATS=3 ./scripts/run_suite.sh 5-8 nfs41
#
# This is the thing to use when the goal is "the same tests on a different
# array". It pins everything except the storage: the same suite, the same
# repeat count, the same git commit, all recorded, so compare_envs.py can
# tell afterwards whether the two runs were actually comparable.
#
# Each test is deployed, collected, parsed and torn down before the next
# begins, so tests never contend with each other.
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

SUITE="${1:-}"
ENVNAME="${2:-}"
NAMESPACE="${3:-fio-tests}"
REPEATS="${REPEATS:-1}"
SETTLE="${SETTLE:-60}"

usage() {
  echo "Usage: ./run_suite.sh <suite|selection> <environment> [namespace]"
  echo
  echo "A selection is test numbers, ranges, or ids -- no config file needed:"
  echo "  ./run_suite.sh 1-4,7-9,12,17 nfs3"
  echo "  ./run_suite.sh 5-8 nfs3"
  echo "  ./run_suite.sh low_qd_latency,sync_write nfs3"
  echo
  echo "Suites:"
  python3 -c "
import json
d = json.load(open(r'$(native_path "$CHART_DIR")/scripts/suites.json'))['suites']
for k in sorted(d): print('  %-14s %d tests: %s' % (k, len(d[k]), ', '.join(d[k])))"
  echo
  echo "Environments:"
  python3 -c "
import json
d = json.load(open(r'$(native_path "$CHART_DIR")/scripts/environments.json'))['environments']
for k in sorted(d): print('  %-14s %-22s %s' % (k, d[k]['storage_class'], d[k].get('description','')))"
  echo
  echo "  REPEATS  runs of each test (default 1; use 3+ for anything quotable)"
  echo "  SETTLE   seconds between runs (default 60)"
  exit 1
}

[ -n "$SUITE" ] && [ -n "$ENVNAME" ] || usage

# ---- resolve suite and environment -----------------------------------
read -r SC ENV_EXTRA < <(python3 - "$(native_path "$CHART_DIR")" "$ENVNAME" <<'PY' | nocr
import json, sys
repo, name = sys.argv[1], sys.argv[2]
envs = json.load(open(repo + "/scripts/environments.json"))["environments"]
if name not in envs:
    sys.exit("unknown environment %r; known: %s" % (name, ", ".join(sorted(envs))))
e = envs[name]
extra = " ".join("%s=%s" % (k, v) for k, v in (e.get("env") or {}).items())
print(e["storage_class"], extra or "-")
PY
) || die "could not resolve environment '$ENVNAME'"
[ "$ENV_EXTRA" = "-" ] && ENV_EXTRA=""

# A named suite from suites.json, or an ad-hoc selection like "1-4,7-9".
#
# Resolved into a variable rather than straight into mapfile: with
# `mapfile < <(...)` the exit status is mapfile's, not the command's, so a
# failure here used to produce an empty test list and a suite that reported
# "0 valid run(s)" as though that were a result.
TESTS_RAW="$(python3 - "$(native_path "$CHART_DIR")" "$SUITE" <<'PY' | nocr
import glob, json, os, sys
sys.path.insert(0, os.path.join(sys.argv[1], "scripts"))
from lib.testselect import SelectionError, resolve_selection

repo, name = sys.argv[1], sys.argv[2]
suites = json.load(open(repo + "/scripts/suites.json"))["suites"]
if name in suites:
    for t in suites[name]:
        print(t)
    sys.exit(0)

available = sorted(
    os.path.basename(f)[:-4]
    for d in ("tests", "profiles")
    for f in glob.glob(os.path.join(repo, "jobs", d, "*.fio")))
try:
    for t in resolve_selection(name, available):
        print(t)
except SelectionError as e:
    sys.exit("%s\n  Named suites: %s" % (e, ", ".join(sorted(suites))))
PY
)" || die "could not resolve '$SUITE'"

mapfile -t TESTS <<< "$TESTS_RAW"
[ ${#TESTS[@]} -gt 0 ] && [ -n "${TESTS[0]}" ] ||   die "'$SUITE' resolved to no tests"

# A named suite keeps its name; an ad-hoc selection is recorded as "manual",
# and its identity is the recorded test list rather than the label.
SUITE_LABEL="$SUITE"
case "$SUITE" in *[,-]*|[0-9]*) SUITE_LABEL="manual" ;; esac
[ -n "${SUITES_JSON_NAME:-}" ] && SUITE_LABEL="$SUITES_JSON_NAME"

# ---- fail before spending the time, not after ------------------------
#
# Everything below is knowable from files on disk in about a second, and each
# check corresponds to a way a finished run gets rejected. The `characterise`
# suite once ran for hours and every test was thrown away at the end with
# "expected 10 pods, got 4". That must be a two-second failure, not a
# two-hour one.
log "validating ${#TESTS[@]} test(s) before deploying anything"
if ! ( cd "$CHART_DIR/scripts" && python3 validate_tests.py --quiet "${TESTS[@]}" ); then
  die "the suite was not started. Every problem above would have rejected a
  healthy run after it finished. Fix the declaration or the job file, then
  re-run. To check without running anything:
    python3 scripts/validate_tests.py"
fi

# Stepped tests produce one result per replica count in step-<n> subdirectories
# rather than one summary, so there is nothing for the suite to record as this
# test's result. Refusing here costs a second; discovering it after a four-step
# curve costs two hours and produces nothing.
STEPPED=()
for t in "${TESTS[@]}"; do
  case "$t" in *gradual_scale*) STEPPED+=("$t") ;; esac
done
if [ ${#STEPPED[@]} -gt 0 ]; then
  die "run_suite.sh cannot run stepped tests: ${STEPPED[*]}
  Each measures several replica counts and writes one result per step, which
  is a scaling curve rather than a single number to compare across arrays.
  Run one directly instead:
    ./scripts/deploy_test.sh ${STEPPED[0]} $NAMESPACE
    python3 scripts/parse_results.py results/<run-id>/step-<n>
  Then drop them from the selection and re-run the rest."
fi
log "validation ok"
echo

STAMP="$(date +%Y%m%d-%H%M%S)"
SUITE_ID="${SUITE_LABEL}-${ENVNAME}-${STAMP}"
SUITE_DIR="$CHART_DIR/results/suites/$SUITE_ID"
mkdir -p "$SUITE_DIR"

log "suite=$SUITE_LABEL (from '$SUITE') environment=$ENVNAME sc=$SC"
log "tests: ${TESTS[*]}"
log "repeats=$REPEATS namespace=$NAMESPACE"
log "results -> results/suites/$SUITE_ID"
echo

PASSED=(); REJECTED=()

for test_id in "${TESTS[@]}"; do
  mkdir -p "$SUITE_DIR/$test_id"
  for rep in $(seq 1 "$REPEATS"); do
    log "=== $test_id  (repeat $rep/$REPEATS, $ENVNAME) ==="

    # shellcheck disable=SC2086
    if ! env STORAGE_CLASS="$SC" $ENV_EXTRA \
         "$CHART_DIR/scripts/deploy_test.sh" "$test_id" "$NAMESPACE"; then
      warn "$test_id repeat $rep: deploy failed, skipping"
      REJECTED+=("$test_id/rep$rep(deploy)")
      continue
    fi

    RID="$(cat "$CHART_DIR/results/.last_run_id")"
    "$CHART_DIR/scripts/collect_results.sh" "$RID" "$NAMESPACE" || \
      warn "$test_id repeat $rep: collection incomplete"

    if ( cd "$CHART_DIR/scripts" && python3 parse_results.py "$CHART_DIR/results/$RID" ); then
      cp "$CHART_DIR/results/$RID/summary_report.json" \
         "$SUITE_DIR/$test_id/rep-${rep}.json"
      PASSED+=("$test_id/rep$rep")
    else
      warn "$test_id repeat $rep: REJECTED, excluded from the suite result"
      REJECTED+=("$test_id/rep$rep")
    fi

    FORCE=true "$CHART_DIR/scripts/cleanup_test.sh" "$RID" "$NAMESPACE" >/dev/null 2>&1 || true

    # Let the array settle so the next run does not inherit this one's
    # cache state and is independent of it.
    sleep "$SETTLE"
    echo
  done
done

git_dirty=false
[ -n "$(git -C "$CHART_DIR" status --porcelain 2>/dev/null)" ] && git_dirty=true
tests_json=$(printf '"%s",' "${TESTS[@]}")

cat > "$SUITE_DIR/suite.json" <<JSON
{
  "suite_id": "$SUITE_ID",
  "suite": "$SUITE_LABEL",
  "selection": "$SUITE",
  "environment": "$ENVNAME",
  "storage_class": "$SC",
  "namespace": "$NAMESPACE",
  "tests": [${tests_json%,}],
  "repeats": $REPEATS,
  "env_overrides": "$ENV_EXTRA",
  "git_commit": "$(git -C "$CHART_DIR" rev-parse HEAD 2>/dev/null || echo unknown)",
  "git_dirty": $git_dirty,
  "started_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "passed": ${#PASSED[@]},
  "rejected": ${#REJECTED[@]}
}
JSON

echo
log "suite complete: ${#PASSED[@]} valid run(s), ${#REJECTED[@]} rejected"
[ ${#REJECTED[@]} -gt 0 ] && warn "rejected: ${REJECTED[*]}"
log "results: results/suites/$SUITE_ID"
echo
log "see what this environment did:"
log "  python3 scripts/suite_report.py results/suites/$SUITE_ID"
echo
log "compare against another environment:"
log "  python3 scripts/compare_envs.py results/suites/<baseline> results/suites/$SUITE_ID"
