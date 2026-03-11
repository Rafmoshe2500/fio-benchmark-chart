#!/bin/bash
set -e

TEST_ID=$1
NAMESPACE=${2:-default}

if [ -z "$TEST_ID" ]; then
  echo "Usage: ./cleanup_test.sh <test_id> [namespace]"
  echo "Example: ./cleanup_test.sh test1_10pods_30k_5050_4kb fio-tests"
  exit 1
fi

echo "Cleaning up resources for Test: $TEST_ID in namespace: $NAMESPACE..."

# We need to find all helm releases associated with this test.
# During deploy, we named releases based on the test ID (e.g. fio-test10-p1, fio-scale, etc)
# To be safe, we will just delete everything that matches the prefix.

HELM_RELEASE="fio-$TEST_ID"
HELM_RELEASE=$(echo "$HELM_RELEASE" | tr '_' '-' | cut -c 1-53)

case $TEST_ID in
  *test10_burst_write*)
    helm uninstall "$HELM_RELEASE-p1" -n "$NAMESPACE" || true
    helm uninstall "$HELM_RELEASE-p2" -n "$NAMESPACE" || true
    ;;
  *test11_burst_read*)
    helm uninstall "$HELM_RELEASE-p1" -n "$NAMESPACE" || true
    helm uninstall "$HELM_RELEASE-p2" -n "$NAMESPACE" || true
    ;;
  *test_example*)
    helm uninstall "$HELM_RELEASE-p1" -n "$NAMESPACE" || true
    helm uninstall "$HELM_RELEASE-p2" -n "$NAMESPACE" || true
    ;;
  *gradual_scale*)
    helm uninstall "fio-scale" -n "$NAMESPACE" || true
    ;;
  *test17_mixed_workload*)
    helm uninstall "$HELM_RELEASE-32k" -n "$NAMESPACE" || true
    helm uninstall "$HELM_RELEASE-64k" -n "$NAMESPACE" || true
    helm uninstall "$HELM_RELEASE-256k" -n "$NAMESPACE" || true
    helm uninstall "$HELM_RELEASE-512k" -n "$NAMESPACE" || true
    ;;
  *)
    helm uninstall "$HELM_RELEASE" -n "$NAMESPACE" || true
    ;;
esac

echo "Helm releases deleted."

# Helm does not always delete the PVCs, so we should clean them up as well if they belong to this prefix
echo "Cleaning up dangling PVCs matching fio-..."
kubectl delete pvc -l "app.kubernetes.io/name=fio-benchmark" -n "$NAMESPACE" || true

echo "Cleanup completed."
