#!/usr/bin/env bash
#
# chaos.sh -- inject failures during a NiFi load run and measure recovery.
#
# A storage system that performs well until something breaks has not been
# shown to be suitable for production. These are the questions this answers:
#
#   How long until the node is serving again after it dies?
#   Did the queue drain back, or did the backlog become permanent?
#   Did any FlowFile get lost, or duplicated?
#
#   ./chaos.sh delete-pod [n]        kill a node under load, time the recovery
#   ./chaos.sh network-fault [secs]  cut the node off from NFS, then restore
#   ./chaos.sh verify-integrity      output vs counters vs input
#   ./chaos.sh campaign [secs]       all of the above in sequence
#
# Run it against a load that is already live:
#   ./nifi-nfs-loadtest.sh run 1800 &
#   sleep 300 && ./chaos.sh campaign
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NS="${NS:-nifi-loadtest}"
REPLICAS="${REPLICAS:-3}"
PORT_BASE="${PORT_BASE:-18443}"
NIFI_USER="${NIFI_USER:-admin}"
NIFI_PASS="${NIFI_PASS:-loadtestAdminPass123}"
HELPER="${HELPER:-$HERE/nififlow.py}"
OUT="${OUT:-$HERE/results}"

log()  { printf '\033[1;35m[chaos %s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }

mkdir -p "$OUT"
STAMP="$(date +%Y%m%d-%H%M%S)"
REPORT="$OUT/chaos-${STAMP}.json"

nfy() {
  local idx="$1"; shift
  python3 "$HELPER" "$1" \
    --url "https://127.0.0.1:$((PORT_BASE+idx))" \
    --user "$NIFI_USER" --password "$NIFI_PASS" \
    --label "nifi-${idx}" "${@:2}"
}

counters_of() { # counters_of <idx> <name>
  nfy "$1" counters 2>/dev/null | awk -v k="$2" '$2==k {print $3}' | head -1
}

queue_of() { nfy "$1" sample 2>/dev/null | cut -d, -f9; }

# ---------------------------------------------------------------- delete-pod
cmd_delete_pod() {
  local idx="${1:-0}" pod="nifi-${1:-0}"
  log "recording state before killing ${pod}"
  local files_before bytes_before q_before
  files_before=$(counters_of "$idx" flowfiles_completed || echo 0)
  bytes_before=$(counters_of "$idx" bytes_completed || echo 0)
  q_before=$(queue_of "$idx" || echo 0)
  log "  counters: ${files_before} files, ${bytes_before} bytes, queue ${q_before}"

  local t0; t0=$(date +%s)
  log "deleting ${pod}"
  kubectl -n "$NS" delete pod "$pod" --wait=false >/dev/null || die "could not delete ${pod}"

  # Ready again: the StatefulSet recreates it and NiFi replays its WAL.
  log "waiting for ${pod} to become Ready"
  local ready_at=0 deadline=$(( t0 + ${RECOVERY_TIMEOUT:-900} ))
  while [[ "$(date +%s)" -lt "$deadline" ]]; do
    if [[ "$(kubectl -n "$NS" get pod "$pod" \
          -o jsonpath='{.status.containerStatuses[0].ready}' 2>/dev/null)" == "true" ]]; then
      ready_at=$(date +%s); break
    fi
    sleep 5
  done
  [[ $ready_at -eq 0 ]] && { warn "${pod} never became Ready"; ready_at=$(date +%s); }
  local ready_s=$(( ready_at - t0 ))
  log "  Ready after ${ready_s}s"

  # Ready is not the same as working. Wait until the counters advance again,
  # which is the first moment the node is actually doing work.
  log "waiting for the flow to resume (counters advancing)"
  local resumed_at=0 files_now
  deadline=$(( $(date +%s) + ${RESUME_TIMEOUT:-600} ))
  while [[ "$(date +%s)" -lt "$deadline" ]]; do
    files_now=$(counters_of "$idx" flowfiles_completed 2>/dev/null || echo "")
    if [[ -n "$files_now" && "$files_now" -gt 0 ]]; then
      resumed_at=$(date +%s); break
    fi
    sleep 5
  done
  local resume_s=0
  if [[ $resumed_at -gt 0 ]]; then
    resume_s=$(( resumed_at - t0 ))
    log "  flow resumed after ${resume_s}s"
  else
    warn "  the flow did not resume within the timeout"
  fi

  cat > "${REPORT%.json}-deletepod.json" <<JSON
{
  "event": "delete-pod",
  "pod": "$pod",
  "at": "$(date -u -d "@$t0" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo "$t0")",
  "ready_after_s": $ready_s,
  "flow_resumed_after_s": $resume_s,
  "files_before": ${files_before:-0},
  "bytes_before": ${bytes_before:-0},
  "queue_before": ${q_before:-0},
  "note": "Counters reset with the pod: NiFi counters do not survive a restart, so files_before is a checkpoint for the integrity check, not a running total."
}
JSON
  log "wrote ${REPORT%.json}-deletepod.json"
}

# ------------------------------------------------------------ network-fault
cmd_network_fault() {
  local secs="${1:-60}" pod="nifi-${FAULT_NODE:-0}"
  log "isolating ${pod} from everything except the API server for ${secs}s"
  warn "this uses a default-deny NetworkPolicy; it requires a CNI that"
  warn "enforces them (Calico, Cilium, OVN). On a CNI that ignores"
  warn "NetworkPolicy this is a no-op and the result is meaningless."

  kubectl apply -f - >/dev/null <<YAML
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: chaos-nfs-blackhole
  namespace: ${NS}
spec:
  podSelector:
    matchLabels:
      statefulset.kubernetes.io/pod-name: ${pod}
  policyTypes: ["Egress"]
  egress:
  - to:
    - namespaceSelector: {}
      podSelector:
        matchLabels:
          k8s-app: kube-dns
    ports: [{ protocol: UDP, port: 53 }]
YAML

  local t0; t0=$(date +%s)
  sleep "$secs"
  kubectl -n "$NS" delete networkpolicy chaos-nfs-blackhole >/dev/null 2>&1 || true
  log "restored connectivity after ${secs}s; watching for recovery"

  local q_peak=0 q
  local deadline=$(( $(date +%s) + ${RECOVERY_TIMEOUT:-600} ))
  while [[ "$(date +%s)" -lt "$deadline" ]]; do
    q=$(queue_of "${FAULT_NODE:-0}" 2>/dev/null || echo "")
    [[ -z "$q" ]] && { sleep 10; continue; }
    [[ "$q" -gt "$q_peak" ]] && q_peak=$q
    [[ "$q" -lt $(( q_peak / 2 )) ]] && { log "  queue draining (peak ${q_peak}, now ${q})"; break; }
    sleep 10
  done

  cat > "${REPORT%.json}-netfault.json" <<JSON
{
  "event": "network-fault",
  "pod": "$pod",
  "duration_s": $secs,
  "queue_peak": $q_peak,
  "recovered_by_s": $(( $(date +%s) - t0 )),
  "caveat": "Only meaningful on a CNI that enforces NetworkPolicy. Verify enforcement before quoting this."
}
JSON
  log "wrote ${REPORT%.json}-netfault.json"
}

# -------------------------------------------------------- verify-integrity
cmd_verify_integrity() {
  log "comparing delivered output against the counters"
  local i files bytes cfiles total_files=0 total_out=0 mismatch=0
  for ((i=0; i<REPLICAS; i++)); do
    files=$(kubectl -n "$NS" exec "nifi-${i}" -c nifi -- \
      bash -c 'find /data/out -type f 2>/dev/null | wc -l' 2>/dev/null || echo 0)
    bytes=$(kubectl -n "$NS" exec "nifi-${i}" -c nifi -- \
      bash -c 'du -sb /data/out 2>/dev/null | cut -f1' 2>/dev/null || echo 0)
    cfiles=$(counters_of "$i" flowfiles_completed 2>/dev/null || echo 0)
    total_files=$(( total_files + ${cfiles:-0} ))
    total_out=$(( total_out + ${files:-0} ))

    # Duplicates are the failure mode a restart produces: NiFi replays its
    # WAL, so a FlowFile in flight when the node died is delivered twice.
    local dupes
    dupes=$(kubectl -n "$NS" exec "nifi-${i}" -c nifi -- \
      bash -c 'find /data/out -type f -printf "%f\n" 2>/dev/null | sort | uniq -d | wc -l' \
      2>/dev/null || echo 0)

    printf '  nifi-%d: counter=%s delivered=%s bytes=%s duplicate-names=%s\n' \
      "$i" "${cfiles:-0}" "${files:-0}" "${bytes:-0}" "${dupes:-0}"
    [[ "${dupes:-0}" -gt 0 ]] && mismatch=1
  done

  local delta=$(( total_out - total_files ))
  echo
  log "counters ${total_files}, delivered ${total_out}, difference ${delta}"
  if [[ ${delta#-} -gt $(( total_files / 100 + 1 )) ]]; then
    warn "the gap exceeds 1%: work was lost or duplicated, or the counters"
    warn "are counting something other than delivered files"
    mismatch=1
  fi

  cat > "${REPORT%.json}-integrity.json" <<JSON
{
  "event": "verify-integrity",
  "counter_files_total": $total_files,
  "delivered_files_total": $total_out,
  "difference": $delta,
  "clean": $([[ $mismatch -eq 0 ]] && echo true || echo false),
  "semantics": "at-least-once. NiFi replays its write-ahead log after a restart, so a FlowFile in flight when a node died is delivered again. Do not describe this flow as exactly-once without a deduplicating sink."
}
JSON
  log "wrote ${REPORT%.json}-integrity.json"
  return 0
}

cmd_campaign() {
  local secs="${1:-60}"
  log "=== failure campaign ==="
  cmd_verify_integrity
  echo
  cmd_delete_pod 0
  echo
  cmd_network_fault "$secs"
  echo
  cmd_verify_integrity
  log "campaign complete; reports in ${OUT}/chaos-${STAMP}-*.json"
}

case "${1:-}" in
  delete-pod)       cmd_delete_pod "${2:-0}" ;;
  network-fault)    cmd_network_fault "${2:-60}" ;;
  verify-integrity) cmd_verify_integrity ;;
  campaign)         cmd_campaign "${2:-60}" ;;
  *) sed -n '3,20p' "$0"; exit 1 ;;
esac
