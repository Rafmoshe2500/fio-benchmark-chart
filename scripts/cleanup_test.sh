#!/bin/bash
set -e

TEST_ID=$1
NAMESPACE=${2:-default}

if [ -z "$TEST_ID" ]; then
  echo "Usage: ./cleanup_test.sh <test_id> [namespace]"
  exit 1
fi

echo "Cleaning up resources for Test: $TEST_ID in namespace: $NAMESPACE..."

HELM_RELEASE="fio-$TEST_ID"
HELM_RELEASE=$(echo "$HELM_RELEASE" | tr '_' '-' | cut -c 1-45)

# פונקציית מחיקה התומכת ב-10G ו-100G
cleanup_dual() {
  local rel_suffix=$1
  helm uninstall "${HELM_RELEASE}${rel_suffix}-10g" -n "$NAMESPACE" || true
  helm uninstall "${HELM_RELEASE}${rel_suffix}-100g" -n "$NAMESPACE" || true
}

case $TEST_ID in
  *test10_burst_write*|*test11_burst_read*|*test_example*)
    cleanup_dual "-p1"
    cleanup_dual "-p2"
    ;;
  *test17_mixed_workload*)
    cleanup_dual "-32k"
    cleanup_dual "-64k"
    cleanup_dual "-256k"
    cleanup_dual "-512k"
    ;;
  *)
    cleanup_dual ""
    ;;
esac

echo "Helm releases deleted."

echo "Cleaning up dangling PVCs matching fio-..."
kubectl delete pvc -l "app.kubernetes.io/name=fio-benchmark" -n "$NAMESPACE" || true

echo "Cleanup completed."