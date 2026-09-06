#!/bin/bash
set -e

TEST_ID=$1
NAMESPACE=${2:-default}

if [ -z "$TEST_ID" ]; then
  echo "Usage: ./collect_results.sh <test_id> [namespace]"
  echo "Example: ./collect_results.sh test1_10pods_30k_5050_4kb fio-tests"
  exit 1
fi

RESULTS_DIR="results/$TEST_ID"
mkdir -p "$RESULTS_DIR"

echo "Collecting results for $TEST_ID in namespace $NAMESPACE..."

# We will grab all pods in the namespace that have a name containing "fio-"
# We can filter by the name prefix used during deployment
PODS=$(kubectl get pods -n "$NAMESPACE" | grep "fio-" | awk '{print $1}')

if [ -z "$PODS" ]; then
  echo "No FIO pods found in namespace $NAMESPACE."
  exit 1
fi

for POD in $PODS; do
  echo "Downloading logs for $POD..."
  kubectl logs "$POD" -n "$NAMESPACE" > "$RESULTS_DIR/${POD}.log"
done

echo "Logs successfully saved to $RESULTS_DIR"
