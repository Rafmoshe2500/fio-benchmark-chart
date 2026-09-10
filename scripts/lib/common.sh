#!/bin/bash
# Shared helpers for the fio benchmark scripts. Source this, do not execute it.
#
# Every Kubernetes name we generate has to be a DNS-1123 label: lowercase
# alphanumerics and hyphens only. The test IDs use underscores, so a single
# sanitised variable is derived once and used for every name.

CHART_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." >/dev/null 2>&1 && pwd)"

RUN_LABEL_KEY="fio.benchmark/run-id"
TEST_LABEL_KEY="fio.benchmark/test-id"

# Paths handed to python3 must be in the platform's native form. On Linux
# that is a no-op; under Git Bash on Windows a /c/... path is meaningless to
# a native Python and has to be converted.
native_path() {
  if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf '%s' "$1"; fi
}

# Strip carriage returns from a pipeline.
#
# A native Windows python3 writes CRLF, and $(...) only strips the trailing
# newline -- so every line but the last comes back with a \r glued to it.
# `run_suite.sh characterise` resolved to 'low_qd_latency\r' and seven other
# names no file matched, which surfaces as "no such fio job file" rather than
# as anything pointing at line endings. Every capture from python3 goes
# through this.
nocr() { tr -d '\r'; }

log()  { printf '\033[1;34m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }

# safe_id <test_id> -> DNS-1123 safe form
safe_id() {
  local s="${1//_/-}"
  s="$(printf '%s' "$s" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9-')"
  s="${s#-}"; s="${s%-}"
  printf '%s' "${s:0:40}"
}

# new_run_id <safe_test_id> -> "<safe>-<timestamp>", <= 63 chars
new_run_id() {
  printf '%s-%s' "$1" "$(date +%Y%m%d-%H%M%S)"
}

# run_selector <run_id> -> label selector string for kubectl
run_selector() {
  printf '%s=%s' "$RUN_LABEL_KEY" "$1"
}

# helm_deploy <release> <namespace> <run_id> [extra helm args...]
# Always upgrade --install so a re-run is idempotent, always --wait so the
# caller knows the pods exist before it does anything that assumes they do,
# always --atomic so a failed deploy does not leave half a test running.
helm_deploy() {
  local release="$1" ns="$2" run_id="$3"; shift 3
  helm upgrade --install "$release" "$CHART_DIR" \
    -n "$ns" --create-namespace \
    --wait --atomic --timeout 15m \
    --set runId="$run_id" \
    "$@"
}

# pvc_size_for <test_id> -> size string, dies if unregistered
pvc_size_for() {
  local id="$1" conf="$CHART_DIR/scripts/pvc_sizes.conf" name size
  while read -r name size _rest; do
    [[ -z "${name:-}" || "${name:0:1}" == "#" ]] && continue
    [[ "$name" == "$id" ]] && { printf '%s' "$size"; return 0; }
  done < "$conf"
  die "no PVC size registered for '$id' in scripts/pvc_sizes.conf.
  Regenerate it with: ./scripts/gen_pvc_sizes.sh"
}

# job_file_for <test_id> -> absolute path, dies if missing
#
# jobs/tests holds the scenario suite; jobs/profiles holds the workload
# matrix (one I/O characteristic each). Both are deployable the same way.
job_file_for() {
  local d f
  for d in tests profiles; do
    f="$CHART_DIR/jobs/$d/$1.fio"
    [[ -f "$f" ]] && { printf '%s' "$f"; return 0; }
  done
  die "no such fio job file: $1.fio (looked in jobs/tests and jobs/profiles)"
}

# meta_dir_for <test_id> -> directory holding its .meta.json
meta_dir_for() {
  local d
  for d in tests profiles; do
    [[ -f "$CHART_DIR/jobs/$d/$1.meta.json" ]] && { printf '%s' "$CHART_DIR/jobs/$d"; return 0; }
  done
  die "no metadata for $1 in jobs/tests or jobs/profiles"
}

# replicas_for <test_id> -> pod count declared in its .meta.json
#
# The single source of truth. This number used to be written twice: here as a
# hardcoded default in deploy_test.sh's case statement, and again in each
# test's .meta.json which parse_results.py validates the run against. They
# drifted for every profile in jobs/profiles, so the `characterise` suite
# deployed 10 pods, the metadata expected 4, and every test was rejected
# after the suite had already run for hours.
replicas_for() {
  local id="$1" md n
  md="$(meta_dir_for "$id")" || die "no metadata for $id"
  [ -n "$md" ] || die "no metadata for $id"
  n="$(python3 -c "
import json, sys
m = json.load(open(sys.argv[1]))
n = m.get('replicas')
if not isinstance(n, int) or n < 1:
    sys.exit('replicas in %s is %r; must be a positive integer' % (sys.argv[1], n))
print(n)" "$(native_path "$md/$id.meta.json")" | nocr)" || die "bad replicas for $id"
  printf '%s' "$n"
}

# resource_class_for <test_id> -> "<cpu> <memory>"
#
# Sized from the observed ceiling run: test7 sustained ~170K IOPS / 665 MiB/s
# of 4K random writes with refill_buffers=1, i.e. two thirds of a GiB per
# second of freshly generated incompressible data. That is CPU work, and at
# 2 cores it is the client that gives out first, not the array.
#
# requests == limits on purpose: that is Guaranteed QoS, the only class the
# kubelet will not throttle first. A throttled client reports CFS stalls as
# storage latency and there is no way to separate them afterwards.
resource_class_for() {
  case "$1" in
    test_example*)                          echo "1 1Gi" ;;
    *1pod_max*|test10_burst*|test11_burst*) echo "8 8Gi" ;;
    *)                                      echo "4 4Gi" ;;
  esac
}

# preflight <test_id> -- refuses to deploy a job that cannot fit its PVC
preflight() {
  local id="$1" job pvc
  job="$(job_file_for "$id")"
  pvc="$(pvc_size_for "$id")"
  ( cd "$CHART_DIR/scripts" && python3 fio_capacity.py "$job" --pvc "$pvc" >/dev/null ) \
    || die "capacity preflight failed for $id"
  log "preflight ok: $id fits $pvc"
}
