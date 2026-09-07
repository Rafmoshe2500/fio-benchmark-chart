#!/usr/bin/env bash
#
# nifi-multi.sh -- run several independent NiFi deployments side by side,
# each in its own namespace, on its own StorageClass, with its own port range.
#
# The usual reason to want this: comparing two StorageClasses (NFSv3 vs
# NFSv4.1, two SVMs, two aggregates) under identical load at the same moment,
# so array-side conditions are shared and the difference is the protocol or
# backend rather than the time of day.
#
#   ./nifi-multi.sh deploy            bring every deployment up
#   ./nifi-multi.sh flow              build and start the load on all of them
#   ./nifi-multi.sh record 1800       record all in parallel, then compare
#   ./nifi-multi.sh compare           re-print the comparison
#   ./nifi-multi.sh stop | start      pause / resume all
#   ./nifi-multi.sh status            pods and PVCs per deployment
#   ./nifi-multi.sh teardown          delete everything
#
# Configuration comes from a file (default ./deployments.conf), one deployment
# per line, whitespace separated, '#' for comments:
#
#     # name    storageclass    replicas  profile
#     nfs3      sc-nas-nfs3     3         smallfile
#     nfs41     sc-nas-nfs41    3         smallfile
#
# Namespaces become nifi-<name>. Port ranges are assigned automatically, 100
# apart, so the deployments never collide on port-forwards.
#
set -euo pipefail

CONF="${CONF:-./deployments.conf}"
DRIVER="${DRIVER:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/nifi-nfs-loadtest.sh}"
PORT_START="${PORT_START:-18443}"
PORT_STRIDE="${PORT_STRIDE:-100}"
OUTDIR="${OUTDIR:-./results}"

log()  { printf '\033[1;36m[multi %s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }

[[ -x "$DRIVER" ]] || die "driver not found or not executable: $DRIVER"

if [[ ! -f "$CONF" ]]; then
  cat >&2 <<EOF
No config at ${CONF}. Create one, for example:

    # name   storageclass    replicas  profile
    nfs3     sc-nas-nfs3     3         smallfile
    nfs41    sc-nas-nfs41    3         smallfile

Then re-run. Override the path with CONF=/path/to/file.
EOF
  exit 1
fi

# ---- parse config into parallel arrays ----
NAMES=(); SCS=(); REPS=(); PROFS=(); PORTS=()
idx=0
while read -r name sc reps prof _rest; do
  [[ -z "${name:-}" || "${name:0:1}" == "#" ]] && continue
  [[ -n "${sc:-}" ]] || die "config line for '${name}' has no StorageClass"
  NAMES+=("$name"); SCS+=("$sc")
  REPS+=("${reps:-3}"); PROFS+=("${prof:-smallfile}")
  PORTS+=($((PORT_START + idx * PORT_STRIDE)))
  idx=$((idx + 1))
done < "$CONF"

[[ ${#NAMES[@]} -gt 0 ]] || die "no deployments defined in ${CONF}"

# env for deployment i
env_for() {
  local i="$1"
  echo "NS=nifi-${NAMES[$i]}" \
       "STORAGE_CLASS=${SCS[$i]}" \
       "REPLICAS=${REPS[$i]}" \
       "PROFILE=${PROFS[$i]}" \
       "PORT_BASE=${PORTS[$i]}"
}

run_for() { # run_for <index> <driver args...>
  local i="$1"; shift
  env $(env_for "$i") "$DRIVER" "$@"
}

show_plan() {
  printf '%-10s %-16s %-4s %-11s %-8s %s\n' NAME STORAGECLASS NODES PROFILE PORTS NAMESPACE
  local i
  for i in "${!NAMES[@]}"; do
    printf '%-10s %-16s %-4s %-11s %-8s %s\n' \
      "${NAMES[$i]}" "${SCS[$i]}" "${REPS[$i]}" "${PROFS[$i]}" \
      "${PORTS[$i]}-$((PORTS[$i] + REPS[$i] - 1))" "nifi-${NAMES[$i]}"
  done
}

cmd_deploy() {
  show_plan; echo
  # Sequential on purpose: parallel provisioning of a dozen PVCs against one
  # array tends to produce confusing timeouts rather than faster deploys.
  local i
  for i in "${!NAMES[@]}"; do
    log "deploying ${NAMES[$i]} on ${SCS[$i]}"
    run_for "$i" deploy || die "deploy failed for ${NAMES[$i]}"
  done
  log "all deployments up"
}

cmd_simple() { # cmd_simple <driver-subcommand>
  local i
  for i in "${!NAMES[@]}"; do
    log "${NAMES[$i]}: $1"
    run_for "$i" "$1" || warn "${NAMES[$i]}: $1 failed"
  done
}

cmd_record() {
  local duration="${1:-1800}"
  mkdir -p "$OUTDIR"
  local stamp; stamp="$(date +%Y%m%d-%H%M%S)"
  local pids=() csvs=() i

  log "recording ${duration}s across ${#NAMES[@]} deployments in parallel"
  for i in "${!NAMES[@]}"; do
    local csv="${OUTDIR}/${NAMES[$i]}-${stamp}.csv"
    csvs+=("$csv")
    # Each child owns its own port range, so the port-forwards do not collide.
    env $(env_for "$i") CSV="$csv" "$DRIVER" record "$duration" \
      > "${OUTDIR}/${NAMES[$i]}-${stamp}.log" 2>&1 &
    pids+=($!)
    log "  ${NAMES[$i]} -> ${csv}"
  done

  log "waiting (per-deployment logs are in ${OUTDIR}/)"
  local rc=0
  for i in "${!pids[@]}"; do
    wait "${pids[$i]}" || { warn "${NAMES[$i]} recorder exited non-zero"; rc=1; }
  done
  echo
  cmd_compare "${csvs[@]}"
  return $rc
}

cmd_compare() {
  local files=("$@")
  if [[ ${#files[@]} -eq 0 ]]; then
    local i
    for i in "${!NAMES[@]}"; do
      local f; f=$(ls -t "${OUTDIR}/${NAMES[$i]}"-*.csv 2>/dev/null | head -1 || true)
      [[ -n "$f" ]] && files+=("$f")
    done
  fi
  [[ ${#files[@]} -gt 0 ]] || die "no CSVs found in ${OUTDIR}/"
  local args=()
  for f in "${files[@]}"; do args+=(--csv "$f"); done
  python3 "$(dirname "$DRIVER")/nififlow.py" compare "${args[@]}"
}

cmd_status() {
  local i
  for i in "${!NAMES[@]}"; do
    echo "===== ${NAMES[$i]}  (ns nifi-${NAMES[$i]}, sc ${SCS[$i]}) ====="
    kubectl -n "nifi-${NAMES[$i]}" get pods -o wide 2>/dev/null || echo "  not deployed"
    kubectl -n "nifi-${NAMES[$i]}" get pvc 2>/dev/null | tail -n +2 | \
      awk '{printf "  %-28s %-8s %s\n", $1, $2, $4}'
    echo
  done
}

cmd_teardown() {
  show_plan; echo
  read -rp "delete ALL of the above, including PVCs? [y/N] " a
  [[ "$a" == "y" ]] || { log "aborted"; exit 0; }
  local i
  for i in "${!NAMES[@]}"; do
    log "deleting nifi-${NAMES[$i]}"
    kubectl delete namespace "nifi-${NAMES[$i]}" --wait=false >/dev/null 2>&1 || true
  done
  log "deletion requested for all namespaces (running in background)"
}

case "${1:-}" in
  plan)     show_plan ;;
  deploy)   cmd_deploy ;;
  flow)     cmd_simple flow ;;
  start)    cmd_simple start ;;
  stop)     cmd_simple stop ;;
  clear)    cmd_simple clear ;;
  record)   cmd_record "${2:-1800}" ;;
  compare)  shift; cmd_compare "$@" ;;
  status)   cmd_status ;;
  teardown) cmd_teardown ;;
  *) sed -n '3,30p' "$0"; exit 1 ;;
esac
