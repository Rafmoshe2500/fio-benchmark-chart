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
#   ./nifi-multi.sh run 1800          FULL lifecycle on all, shared start
#   ./nifi-multi.sh record 1800       record all in parallel, then compare
#   ./nifi-multi.sh compare           re-print the comparison
#   ./nifi-multi.sh stop | start      pause / resume all
#   ./nifi-multi.sh status            pods and PVCs per deployment
#   ./nifi-multi.sh teardown          delete everything
#
# Configuration is ./deployments.json (preferred) or ./deployments.conf.
# JSON supports defaults plus per-deployment overrides of any driver variable:
#
#     {
#       "defaults": { "storageClass": "sc-nas-nfs3", "replicas": 2 },
#       "deployments": [
#         { "name": "env1" },
#         { "name": "env2", "profile": "bigfile",
#           "env": { "CONTENT_REPO_SIZE": "100Gi" } }
#       ]
#     }
#
# Namespaces become nifi-<name>. Port ranges are assigned automatically, 100
# apart, so the deployments never collide on port-forwards.
#
set -euo pipefail

# Prefer JSON when both are present; the whitespace format stays supported.
if [[ -z "${CONF:-}" ]]; then
  _here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if   [[ -f "$_here/deployments.json" ]]; then CONF="$_here/deployments.json"
  elif [[ -f "$_here/deployments.conf" ]]; then CONF="$_here/deployments.conf"
  else CONF="$_here/deployments.json"
  fi
fi
DRIVER="${DRIVER:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/nifi-nfs-loadtest.sh}"
PORT_START="${PORT_START:-18443}"
PORT_STRIDE="${PORT_STRIDE:-100}"
# Script-relative, like CONF and DRIVER. A cwd-relative ./results put the
# NiFi CSVs into the fio results directory whenever this was run from the
# repository root, mixing two unrelated result sets in one place.
OUTDIR="${OUTDIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/results}"

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

# ---- parse config ----------------------------------------------------
# Two formats are accepted. JSON (recommended) supports defaults plus
# per-deployment overrides of any driver variable; the whitespace format is
# kept so older configs keep working.
NAMES=(); SCS=(); REPS=(); PROFS=(); PORTS=(); EXTRA=()

parse_config() {
  local first; first="$(grep -m1 -v '^\s*$' "$CONF" | head -c1 || true)"
  if [[ "$CONF" == *.json || "$first" == "{" ]]; then
    parse_json
  else
    parse_table
  fi
}

parse_table() {
  local idx=0 name sc reps prof _rest
  while read -r name sc reps prof _rest; do
    [[ -z "${name:-}" || "${name:0:1}" == "#" ]] && continue
    [[ -n "${sc:-}" ]] || die "config line for '${name}' has no StorageClass"
    NAMES+=("$name"); SCS+=("$sc")
    REPS+=("${reps:-3}"); PROFS+=("${prof:-smallfile}")
    PORTS+=($((PORT_START + idx * PORT_STRIDE))); EXTRA+=("")
    idx=$((idx + 1))
  done < "$CONF"
}

parse_json() {
  local parsed
  parsed="$(python3 - "$CONF" "$PORT_START" "$PORT_STRIDE" <<'PYEOF' | tr -d '\r'
import json, shlex, sys

path, port_start, stride = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
try:
    doc = json.load(open(path))
except json.JSONDecodeError as e:
    sys.exit(f"invalid JSON in {path}: {e}")

if not isinstance(doc, dict) or "deployments" not in doc:
    sys.exit(f"{path}: expected an object with a 'deployments' array")

defaults = doc.get("defaults") or {}
deps = doc["deployments"]
if not isinstance(deps, list) or not deps:
    sys.exit(f"{path}: 'deployments' must be a non-empty array")

seen = set()
for i, d in enumerate(deps):
    if not isinstance(d, dict):
        sys.exit(f"{path}: deployment #{i+1} is not an object")
    name = d.get("name")
    if not name:
        sys.exit(f"{path}: deployment #{i+1} has no 'name'")
    if name in seen:
        sys.exit(f"{path}: duplicate deployment name {name!r}")
    seen.add(name)

    sc = d.get("storageClass", defaults.get("storageClass"))
    if not sc:
        sys.exit(f"{path}: {name} has no storageClass and no default")
    reps = d.get("replicas", defaults.get("replicas", 3))
    prof = d.get("profile", defaults.get("profile", "smallfile"))
    try:
        reps = int(reps)
        if reps < 1:
            raise ValueError
    except (TypeError, ValueError):
        sys.exit(f"{path}: {name}: replicas must be a positive integer")
    if prof not in ("smallfile", "bigfile", "churn"):
        sys.exit(f"{path}: {name}: unknown profile {prof!r}")
    if reps > stride:
        sys.exit(f"{path}: {name}: replicas ({reps}) exceeds the port stride "
                 f"({stride}); raise PORT_STRIDE or the port ranges will overlap")

    # defaults.env merged first, then this deployment's env wins
    env = dict(defaults.get("env") or {})
    env.update(d.get("env") or {})
    envstr = " ".join(f"{k}={shlex.quote(str(v))}" for k, v in env.items())

    print("\t".join([name, sc, str(reps), prof,
                     str(port_start + i * stride), envstr]))
PYEOF
  )" || die "config rejected (see the message above)"

  while IFS=$'\t' read -r name sc reps prof port envstr; do
    [[ -z "$name" ]] && continue
    NAMES+=("$name"); SCS+=("$sc"); REPS+=("$reps")
    PROFS+=("$prof"); PORTS+=("$port"); EXTRA+=("$envstr")
  done <<< "$parsed"
}

parse_config

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
  # EXTRA holds shell-quoted K=V pairs from the config; eval applies them
  # after the positional settings so a per-deployment override wins.
  # Word splitting on env_for is deliberate: it emits discrete K=V tokens.
  # shellcheck disable=SC2046
  eval env $(env_for "$i") ${EXTRA[$i]} '"$DRIVER"' '"$@"'
}

show_plan() {
  printf '%-10s %-16s %-5s %-10s %-13s %s\n' NAME STORAGECLASS NODES PROFILE PORTS NAMESPACE
  local i
  for i in "${!NAMES[@]}"; do
    printf '%-10s %-16s %-5s %-10s %-13s %s\n' \
      "${NAMES[$i]}" "${SCS[$i]}" "${REPS[$i]}" "${PROFS[$i]}" \
      "${PORTS[$i]}-$((PORTS[$i] + REPS[$i] - 1))" "nifi-${NAMES[$i]}"
    [[ -n "${EXTRA[$i]}" ]] && printf '%12s overrides: %s\n' "" "${EXTRA[$i]}"
  done
  return 0
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

  # One absolute instant shared by every child, so the deployments are under
  # load simultaneously rather than staggered by their own setup time. The
  # whole point of running them together is that array-side conditions are
  # shared; a skewed start throws that away.
  local start_epoch=$(( $(date +%s) + ${BARRIER_LEAD:-120} ))
  log "recording ${duration}s across ${#NAMES[@]} deployments in parallel"
  log "synchronised start at $(date -d "@$start_epoch" 2>/dev/null || date -r "$start_epoch")"
  for i in "${!NAMES[@]}"; do
    local csv="${OUTDIR}/${NAMES[$i]}-${stamp}.csv"
    csvs+=("$csv")
    # Each child owns its own port range, so the port-forwards do not collide.
    # shellcheck disable=SC2046
    eval env $(env_for "$i") ${EXTRA[$i]} CSV='"$csv"' START_EPOCH='"$start_epoch"' '"$DRIVER"' record "$duration" \
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

# Full lifecycle across every deployment, sharing one start instant.
cmd_run() {
  local duration="${1:-1800}"
  local start_epoch=$(( $(date +%s) + ${BARRIER_LEAD:-300} ))
  mkdir -p "$OUTDIR"
  local stamp; stamp="$(date +%Y%m%d-%H%M%S)"
  local pids=() csvs=() i
  log "full lifecycle across ${#NAMES[@]} deployments"
  log "load starts at $(date -d "@$start_epoch" 2>/dev/null || date -r "$start_epoch")"
  for i in "${!NAMES[@]}"; do
    local csv="${OUTDIR}/${NAMES[$i]}-${stamp}.csv"
    csvs+=("$csv")
    # shellcheck disable=SC2046
    eval env $(env_for "$i") ${EXTRA[$i]} CSV='"$csv"' START_EPOCH='"$start_epoch"' '"$DRIVER"' run "$duration" > "${OUTDIR}/${NAMES[$i]}-${stamp}.log" 2>&1 &
    pids+=($!)
    log "  ${NAMES[$i]} -> ${csv}"
  done
  local rc=0
  for i in "${!pids[@]}"; do
    wait "${pids[$i]}" || { warn "${NAMES[$i]} exited non-zero"; rc=1; }
  done
  echo
  cmd_compare "${csvs[@]}"
  return $rc
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
  run)      cmd_run "${2:-1800}" ;;
  record)   cmd_record "${2:-1800}" ;;
  compare)  shift; cmd_compare "$@" ;;
  status)   cmd_status ;;
  teardown) cmd_teardown ;;
  *) sed -n '3,30p' "$0"; exit 1 ;;
esac
