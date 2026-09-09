#!/bin/bash
# Shared helpers for the fio benchmark scripts. Source this, do not execute it.
#
# Every Kubernetes name we generate has to be a DNS-1123 label: lowercase
# alphanumerics and hyphens only. The test IDs use underscores, so a single
# sanitised variable is derived once and used for every name.

CHART_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." >/dev/null 2>&1 && pwd)"

RUN_LABEL_KEY="fio.benchmark/run-id"
TEST_LABEL_KEY="fio.benchmark/test-id"

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
