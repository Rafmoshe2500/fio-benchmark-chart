#!/bin/bash
# Deliberately not `set -e`: one unreadable pod must not abort the collection
# and leave the rest of the run on a cluster that is about to be torn down.
set -uo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

RUN_ID="${1:-}"
NAMESPACE="${2:-fio-tests}"
SUBDIR="${3:-}"

if [ -z "$RUN_ID" ]; then
  echo "Usage: ./collect_results.sh <run_id> [namespace] [subdir]"
  echo "Run IDs are printed by deploy_test.sh and stored in results/<run_id>/manifest.json"
  exit 1
fi

RESULTS_DIR="$CHART_DIR/results/$RUN_ID${SUBDIR:+/$SUBDIR}"
mkdir -p "$RESULTS_DIR"
SEL="$(run_selector "$RUN_ID")"

log "collecting run $RUN_ID from namespace $NAMESPACE"

# Wait for every pod to reach a terminal state. Collecting a Running pod
# yields a truncated log with no closing JSON marker, which the parser will
# reject anyway; waiting is cheaper than a rerun.
DEADLINE=$(( $(date +%s) + ${COLLECT_TIMEOUT:-3600} ))
while true; do
  TOTAL=$(kubectl get pods -n "$NAMESPACE" -l "$SEL" -o name 2>/dev/null | wc -l)
  [ "$TOTAL" -eq 0 ] && die "no pods carry label $SEL in namespace $NAMESPACE"
  ACTIVE=$(kubectl get pods -n "$NAMESPACE" -l "$SEL" \
    --field-selector 'status.phase=Running' -o name 2>/dev/null | wc -l)
  PENDING=$(kubectl get pods -n "$NAMESPACE" -l "$SEL" \
    --field-selector 'status.phase=Pending' -o name 2>/dev/null | wc -l)
  [ $((ACTIVE + PENDING)) -eq 0 ] && break
  [ "$(date +%s)" -gt "$DEADLINE" ] && { warn "timed out: $ACTIVE running, $PENDING pending"; break; }
  printf '\r  waiting: %d running, %d pending, %d total' "$ACTIVE" "$PENDING" "$TOTAL"
  sleep 15
done
echo

# Environment first. The logs are not interpretable without knowing which
# nodes, which storage class and which events produced them.
kubectl get pods -n "$NAMESPACE" -l "$SEL" -o json  > "$RESULTS_DIR/pods.json" 2>/dev/null
kubectl get pvc  -n "$NAMESPACE" -l "$SEL" -o json  > "$RESULTS_DIR/pvcs.json" 2>/dev/null
kubectl get events -n "$NAMESPACE" --sort-by=.lastTimestamp > "$RESULTS_DIR/events.txt" 2>/dev/null
kubectl get nodes -o json > "$RESULTS_DIR/nodes.json" 2>/dev/null

FAILED=0; COLLECTED=0; RESTARTED=0; BADEXIT=0
PODS=$(kubectl get pods -n "$NAMESPACE" -l "$SEL" -o jsonpath='{.items[*].metadata.name}' 2>/dev/null)
EXPECTED=$(echo "$PODS" | wc -w)

for POD in $PODS; do
  if kubectl logs "$POD" -n "$NAMESPACE" > "$RESULTS_DIR/${POD}.log" 2>/dev/null; then
    COLLECTED=$((COLLECTED + 1))
  else
    warn "could not read logs for $POD"
    FAILED=$((FAILED + 1))
    continue
  fi

  RC=$(kubectl get pod "$POD" -n "$NAMESPACE" \
    -o jsonpath='{.status.containerStatuses[0].state.terminated.exitCode}' 2>/dev/null)
  RS=$(kubectl get pod "$POD" -n "$NAMESPACE" \
    -o jsonpath='{.status.containerStatuses[0].restartCount}' 2>/dev/null)
  RSN=$(kubectl get pod "$POD" -n "$NAMESPACE" \
    -o jsonpath='{.status.containerStatuses[0].state.terminated.reason}' 2>/dev/null)

  # A pod that restarted ran fio twice; its numbers describe neither run.
  if [ "${RS:-0}" -gt 0 ] 2>/dev/null; then
    warn "$POD restarted ${RS}x -- its results describe neither run"
    RESTARTED=$((RESTARTED + 1))
  fi
  if [ -n "${RC:-}" ] && [ "$RC" != "0" ]; then
    if [ "$RC" = "28" ]; then
      warn "$POD exited 28: the capacity gate refused the job (PVC too small)"
    else
      warn "$POD exited $RC (${RSN:-unknown})"
    fi
    BADEXIT=$((BADEXIT + 1))
  fi
done

cat > "$RESULTS_DIR/collection.json" <<JSON
{
  "run_id": "$RUN_ID",
  "namespace": "$NAMESPACE",
  "collected_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "pods_expected": $EXPECTED,
  "pods_collected": $COLLECTED,
  "pods_log_failed": $FAILED,
  "pods_restarted": $RESTARTED,
  "pods_bad_exit": $BADEXIT
}
JSON

# Each step subdirectory needs its own copy of the manifest so it can be
# parsed on its own.
if [ -n "$SUBDIR" ] && [ -f "$CHART_DIR/results/$RUN_ID/manifest.json" ]; then
  cp "$CHART_DIR/results/$RUN_ID/manifest.json" "$RESULTS_DIR/" 2>/dev/null
fi

log "collected $COLLECTED/$EXPECTED pods to $RESULTS_DIR"
if [ $((FAILED + RESTARTED + BADEXIT)) -gt 0 ]; then
  warn "run is NOT clean: $FAILED unreadable, $RESTARTED restarted, $BADEXIT bad exit"
  warn "parse_results.py will reject it -- that is the intended behaviour"
fi
log "report with: python3 scripts/parse_results.py results/$RUN_ID${SUBDIR:+/$SUBDIR}"
