#!/bin/bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

TEST_ID="${1:-}"
NAMESPACE="${2:-fio-tests}"

if [ -z "$TEST_ID" ]; then
  echo "Usage: ./deploy_test.sh <test_id> [namespace]"
  echo "Example: ./deploy_test.sh test1_10pods_30k_5050_4kb fio-tests"
  echo
  echo "Environment:"
  echo "  STORAGE_CLASS   storage class to test        (default sc-nas-nfs3)"
  echo "  BARRIER_LEAD    seconds before synchronised start (default 180)"
  echo
  echo "Scenarios (jobs/tests):"
  for f in "$CHART_DIR"/jobs/tests/*.fio; do echo "  $(basename "$f" .fio)"; done
  echo
  echo "Workload profiles (jobs/profiles):"
  for f in "$CHART_DIR"/jobs/profiles/*.fio; do echo "  $(basename "$f" .fio)"; done
  exit 1
fi

# Accept the same shorthand run_suite.sh takes, so "test7" and "7" both work
# rather than only the full test7_1pod_max_write_4kb. A shorthand that names
# more than one test is refused here: deploy_test.sh deploys one test, and
# quietly picking the first would be worse than saying so.
if [ ! -f "$CHART_DIR/jobs/tests/$TEST_ID.fio" ] &&    [ ! -f "$CHART_DIR/jobs/profiles/$TEST_ID.fio" ]; then
  RESOLVED="$(python3 - "$(native_path "$CHART_DIR")" "$TEST_ID" <<'PYSEL' | nocr
import glob, os, sys
sys.path.insert(0, os.path.join(sys.argv[1], "scripts"))
from lib.testselect import SelectionError, resolve_selection

repo, spec = sys.argv[1], sys.argv[2]
spec = spec[4:] if spec.startswith("test") and spec[4:].isdigit() else spec
available = sorted(
    os.path.basename(f)[:-4]
    for d in ("tests", "profiles")
    for f in glob.glob(os.path.join(repo, "jobs", d, "*.fio")))
try:
    picked = resolve_selection(spec, available)
except SelectionError as e:
    sys.exit(str(e))
if len(picked) > 1:
    sys.exit("%r matches %d tests: %s"
             % (sys.argv[2], len(picked), ", ".join(picked))
             + "\n  deploy_test.sh runs one test. Use run_suite.sh for a set.")
print(picked[0])
PYSEL
)" || die "could not resolve '$TEST_ID'"
  log "resolved '$TEST_ID' -> $RESOLVED"
  TEST_ID="$RESOLVED"
fi

SAFE_TEST_ID="$(safe_id "$TEST_ID")"
RUN_ID="$(new_run_id "$SAFE_TEST_ID")"
STORAGE_CLASS="${STORAGE_CLASS:-sc-nas-nfs3}"

# Every pod waits for this absolute instant before starting fio, so all
# releases in a multi-release test begin together no matter when helm
# returned. 180s covers PVC binding and image pull on a cold node.
BARRIER_LEAD="${BARRIER_LEAD:-180}"
START_EPOCH=$(( $(date +%s) + BARRIER_LEAD ))

RESULTS_DIR="$CHART_DIR/results/$RUN_ID"
mkdir -p "$RESULTS_DIR"

log "test=$TEST_ID"
log "run=$RUN_ID"
log "ns=$NAMESPACE sc=$STORAGE_CLASS"
log "synchronised start at $(date -d "@$START_EPOCH" 2>/dev/null || date -r "$START_EPOCH" 2>/dev/null || echo "epoch $START_EPOCH")"

RELEASES=()

# deploy_release <release-suffix> <test_id_for_job_file> [extra helm args...]
#
# The pod count is NOT an argument. It comes from the job's .meta.json, which
# is the same file parse_results.py validates the finished run against. When
# it was passed here as well the two disagreed for every profile and whole
# suites were rejected after running.
deploy_release() {
  local suffix="$1" job_id="$2"; shift 2
  local replicas
  replicas="$(replicas_for "$job_id")"

  preflight "$job_id"

  local cpu mem
  read -r cpu mem <<< "$(resource_class_for "$job_id")"

  # Read tests declare allow_file_create=0 and need their dataset laid out
  # first, otherwise they read sparse files and measure nothing.
  local prep=false
  case "$job_id" in
    test8_1pod_max_read_4kb|test11_burst_read_phase2|cold_read|warm_read) prep=true ;;
  esac

  local prefix="fio-${SAFE_TEST_ID}${suffix:+-$suffix}"
  local release="${prefix:0:53}"

  log "deploying $release: $replicas pods, job $job_id, ${cpu} cpu / ${mem}"
  helm_deploy "$release" "$NAMESPACE" "$RUN_ID" \
    --set replicaCount="$replicas" \
    --set namePrefix="$prefix" \
    --set startEpoch="$START_EPOCH" \
    --set prepare.enabled="$prep" \
    --set pvc.storageClassName="$STORAGE_CLASS" \
    --set pvc.size="$(pvc_size_for "$job_id")" \
    --set resources.requests.cpu="$cpu" --set resources.limits.cpu="$cpu" \
    --set resources.requests.memory="$mem" --set resources.limits.memory="$mem" \
    --set-file fioJob.content="$(job_file_for "$job_id")" \
    "$@"
  RELEASES+=("$release")
}

# Only tests that deploy MORE THAN ONE release need a branch here. A plain
# test needs no entry: how many pods it wants is in its metadata.
case $TEST_ID in
  *test10_burst_write*)
    # Both phases are deployed up front so PVCs are bound and images pulled
    # before either starts. The barrier, not a sleep, staggers them: the old
    # `sleep 300` measured time since helm returned, which is not the same
    # thing as time since phase 1 began generating load.
    deploy_release "p1" test10_burst_write_phase1
    deploy_release "p2" test10_burst_write_phase2 \
      --set startEpoch=$((START_EPOCH + 300))
    ;;

  *test11_burst_read*)
    deploy_release "p1" test11_burst_read_phase1
    deploy_release "p2" test11_burst_read_phase2 \
      --set startEpoch=$((START_EPOCH + 300))
    ;;

  *test_example*)
    deploy_release "p1" test_example_phase1
    deploy_release "p2" test_example_phase2 \
      --set startEpoch=$((START_EPOCH + 120))
    ;;

  *gradual_scale*)
    exec "$CHART_DIR/scripts/deploy_scale_steps.sh" "$TEST_ID" "$NAMESPACE" "$RUN_ID"
    ;;

  *test17_mixed_workload*)
    deploy_release "32k"  test17_mixed_workload_32k
    deploy_release "64k"  test17_mixed_workload_64k
    deploy_release "256k" test17_mixed_workload_256k
    deploy_release "512k" test17_mixed_workload_512k
    ;;

  *)
    deploy_release "" "$TEST_ID"
    ;;
esac

# The manifest is what makes a run reproducible and what collect_results.sh
# and parse_results.py validate against. Without it a set of logs is just a
# set of logs.
releases_json=$(printf '"%s",' "${RELEASES[@]}")
git_dirty=false
[ -n "$(git -C "$CHART_DIR" status --porcelain 2>/dev/null)" ] && git_dirty=true

cat > "$RESULTS_DIR/manifest.json" <<JSON
{
  "run_id": "$RUN_ID",
  "test_id": "$TEST_ID",
  "namespace": "$NAMESPACE",
  "storage_class": "$STORAGE_CLASS",
  "releases": [${releases_json%,}],
  "start_epoch": $START_EPOCH,
  "barrier_lead_s": $BARRIER_LEAD,
  "git_commit": "$(git -C "$CHART_DIR" rev-parse HEAD 2>/dev/null || echo unknown)",
  "git_dirty": $git_dirty,
  "deployed_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
JSON

# Machine-readable handoff. Callers must not have to scrape the log for the
# run id -- run_repeated.sh depends on this file.
printf '%s\n' "$RUN_ID" > "$CHART_DIR/results/.last_run_id"

log "manifest: $RESULTS_DIR/manifest.json"
echo
log "next:  ./scripts/collect_results.sh $RUN_ID $NAMESPACE"
log "then:  python3 scripts/parse_results.py results/$RUN_ID"
log "clean: ./scripts/cleanup_test.sh $RUN_ID $NAMESPACE"
