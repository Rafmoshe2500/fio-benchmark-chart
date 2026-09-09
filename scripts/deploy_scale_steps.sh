#!/bin/bash
# Stepped scaling: each replica count gets its own complete run -- deploy,
# synchronised start, full measurement window, drain, teardown -- before the
# next step begins.
#
# The old approach added five pods a minute for thirty minutes while every
# pod ran an 1800s job from the moment it was created. Early pods finished
# while late ones were still starting, so no two pods ever measured the same
# concurrency and no summary described a stable replica count. A scaling
# curve needs one stable window per point, not a moving target.
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

TEST_ID="${1:-}"
NAMESPACE="${2:-fio-tests}"
RUN_ID="${3:-}"

[ -n "$TEST_ID" ] || die "Usage: ./deploy_scale_steps.sh <test_id> <namespace> <run_id>"

SAFE_TEST_ID="$(safe_id "$TEST_ID")"
[ -n "$RUN_ID" ] || RUN_ID="$(new_run_id "$SAFE_TEST_ID")"

STORAGE_CLASS="${STORAGE_CLASS:-sc-nas-nfs3}"
STEPS="${STEPS:-10 20 40 80}"
BARRIER_LEAD="${BARRIER_LEAD:-180}"
STEP_TIMEOUT="${STEP_TIMEOUT:-1800}"

RESULTS_DIR="$CHART_DIR/results/$RUN_ID"
mkdir -p "$RESULTS_DIR"

preflight "$TEST_ID"
PVC="$(pvc_size_for "$TEST_ID")"
JOB="$(job_file_for "$TEST_ID")"
read -r CPU MEM <<< "$(resource_class_for "$TEST_ID")"

log "stepped scaling for $TEST_ID: steps [$STEPS]"
log "run=$RUN_ID ns=$NAMESPACE sc=$STORAGE_CLASS pvc=$PVC"

STEP_JSON=""

for n in $STEPS; do
  release="fio-${SAFE_TEST_ID}-s${n}"
  release="${release:0:53}"
  start_epoch=$(( $(date +%s) + BARRIER_LEAD ))

  log "=== step ${n} pods ==="
  helm_deploy "$release" "$NAMESPACE" "$RUN_ID" \
    --set replicaCount="$n" \
    --set namePrefix="fio-${SAFE_TEST_ID}-s${n}" \
    --set startEpoch="$start_epoch" \
    --set pvc.storageClassName="$STORAGE_CLASS" \
    --set pvc.size="$PVC" \
    --set resources.requests.cpu="$CPU" --set resources.limits.cpu="$CPU" \
    --set resources.requests.memory="$MEM" --set resources.limits.memory="$MEM" \
    --set-file fioJob.content="$JOB"

  log "step $n: waiting for all $n pods to finish"
  deadline=$(( $(date +%s) + STEP_TIMEOUT ))
  while true; do
    active=$(kubectl get pods -n "$NAMESPACE" -l "$(run_selector "$RUN_ID")" \
      --field-selector 'status.phase=Running' -o name 2>/dev/null | wc -l)
    pending=$(kubectl get pods -n "$NAMESPACE" -l "$(run_selector "$RUN_ID")" \
      --field-selector 'status.phase=Pending' -o name 2>/dev/null | wc -l)
    [ $((active + pending)) -eq 0 ] && break
    [ "$(date +%s)" -gt "$deadline" ] && { warn "step $n timed out: $active running, $pending pending"; break; }
    printf '\r  %d running, %d pending' "$active" "$pending"
    sleep 15
  done
  echo

  "$CHART_DIR/scripts/collect_results.sh" "$RUN_ID" "$NAMESPACE" "step-$n" || \
    warn "collection incomplete for step $n"

  STEP_JSON="${STEP_JSON}{\"replicas\":${n},\"release\":\"${release}\",\"start_epoch\":${start_epoch},\"end_epoch\":$(date +%s),\"subdir\":\"step-${n}\"},"

  log "step $n: tearing down before the next step"
  helm uninstall "$release" -n "$NAMESPACE" >/dev/null 2>&1 || true
  kubectl delete pvc -n "$NAMESPACE" -l "$(run_selector "$RUN_ID")" --wait=true >/dev/null 2>&1 || true
done

printf '{"run_id":"%s","test_id":"%s","steps":[%s]}\n' \
  "$RUN_ID" "$TEST_ID" "${STEP_JSON%,}" > "$RESULTS_DIR/steps.json"

git_dirty=false
[ -n "$(git -C "$CHART_DIR" status --porcelain 2>/dev/null)" ] && git_dirty=true
cat > "$RESULTS_DIR/manifest.json" <<JSON
{
  "run_id": "$RUN_ID",
  "test_id": "$TEST_ID",
  "namespace": "$NAMESPACE",
  "storage_class": "$STORAGE_CLASS",
  "releases": [],
  "stepped": true,
  "steps": [$(echo "$STEPS" | tr ' ' ',')],
  "start_epoch": 0,
  "git_commit": "$(git -C "$CHART_DIR" rev-parse HEAD 2>/dev/null || echo unknown)",
  "git_dirty": $git_dirty,
  "deployed_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
JSON

printf '%s\n' "$RUN_ID" > "$CHART_DIR/results/.last_run_id"
log "scaling curve complete: $RESULTS_DIR/steps.json"
log "each step has its own subdirectory: results/$RUN_ID/step-<n>/"
