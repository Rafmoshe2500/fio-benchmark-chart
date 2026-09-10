#!/bin/bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

RUN_ID="${1:-}"
NAMESPACE="${2:-fio-tests}"
FORCE="${FORCE:-false}"

if [ -z "$RUN_ID" ]; then
  echo "Usage: ./cleanup_test.sh <run_id> [namespace]"
  echo "Run IDs are printed by deploy_test.sh and stored in results/<run_id>/manifest.json"
  echo
  echo "Runs currently present in $NAMESPACE:"
  kubectl get pods -n "$NAMESPACE" \
    -o jsonpath="{range .items[*]}{.metadata.labels['fio\.benchmark/run-id']}{'\n'}{end}" \
    2>/dev/null | sort -u | grep -v '^$' | sed 's/^/  /' || echo "  (none)"
  exit 1
fi

SEL="$(run_selector "$RUN_ID")"

# Show what will go before anything goes. The old script deleted every PVC
# matching app.kubernetes.io/name=fio-benchmark across the namespace, which
# took out concurrent runs and prepared read datasets with them.
echo "Resources matching ${SEL} in ${NAMESPACE}:"
kubectl get pods,pvc,configmap -n "$NAMESPACE" -l "$SEL" 2>/dev/null || true
echo

if [ "$FORCE" != "true" ]; then
  read -rp "Delete all of the above? [y/N] " a
  [[ "$a" == "y" ]] || { log "aborted"; exit 0; }
fi

# Uninstall by release name recorded at deploy time, not guessed. The old
# gradual-scale branch uninstalled a hardcoded "fio-scale" while deploy had
# installed a release named after the test, so the release survived cleanup.
MANIFEST="$CHART_DIR/results/$RUN_ID/manifest.json"
if [ -f "$MANIFEST" ]; then
  while read -r release; do
    [ -z "$release" ] && continue
    log "uninstalling $release"
    helm uninstall "$release" -n "$NAMESPACE" 2>/dev/null || warn "$release already gone"
  done < <(python3 -c 'import json,sys
for r in json.load(open(sys.argv[1])).get("releases", []): print(r)' "$MANIFEST" | nocr)
else
  warn "no manifest for $RUN_ID; falling back to label-scoped deletion only"
fi

# Scoped to this run and nothing else.
kubectl delete pvc -n "$NAMESPACE" -l "$SEL" --wait=false 2>/dev/null || true

log "cleanup completed for $RUN_ID"
