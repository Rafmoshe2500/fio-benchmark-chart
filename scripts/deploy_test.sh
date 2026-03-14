#!/bin/bash
set -e

CHART_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." >/dev/null 2>&1 && pwd)"
TEST_ID=$1
NAMESPACE=${2:-default}

if [ -z "$TEST_ID" ]; then
  echo "Usage: ./deploy_test.sh <test_id> [namespace]"
  echo "Example: ./deploy_test.sh test1_10pods_30k_5050_4kb fio-tests"
  exit 1
fi

echo "Deploying Benchmark: $TEST_ID in namespace: $NAMESPACE"

# Define default helm parameters
REPLICAS=10
HELM_RELEASE="fio-$TEST_ID"
HELM_RELEASE=$(echo "$HELM_RELEASE" | tr '_' '-' | cut -c 1-53)

# Logic to handle specific test cases that require different replica counts or multiple phases
case $TEST_ID in
  *1pod*)
    REPLICAS=1
    helm install "$HELM_RELEASE" "$CHART_DIR" -n "$NAMESPACE" --create-namespace \
      --set replicaCount=$REPLICAS \
      --set namePrefix="fio-$TEST_ID" \
      --set-file fioJob.content="$CHART_DIR/jobs/tests/$HELM_RELEASE.fio"
    ;;
  
  *test10_burst_write*)
    echo "Deploying Phase 1 (5 Pods)..."
    helm install "$HELM_RELEASE-p1" "$CHART_DIR" -n "$NAMESPACE" --create-namespace \
      --set replicaCount=5 --set namePrefix="fio-t10-p1" \
      --set-file fioJob.content="$CHART_DIR/jobs/tests/test10_burst_write_phase1.fio"
    
    echo "Waiting 5 minutes for Burst Phase 2..."
    sleep 300
    
    echo "Deploying Phase 2 (+5 Pods)..."
    helm install "$HELM_RELEASE-p2" "$CHART_DIR" -n "$NAMESPACE" --create-namespace \
      --set replicaCount=5 --set namePrefix="fio-t10-p2" \
      --set-file fioJob.content="$CHART_DIR/jobs/tests/test10_burst_write_phase2.fio"
    ;;
    
  *test11_burst_read*)
    echo "Deploying Phase 1 (20 Pods)..."
    helm install "$HELM_RELEASE-p1" "$CHART_DIR" -n "$NAMESPACE" --create-namespace \
      --set replicaCount=20 --set namePrefix="fio-t11-p1" \
      --set-file fioJob.content="$CHART_DIR/jobs/tests/test11_burst_read_phase1.fio"
    
    echo "Waiting 5 minutes for Burst Phase 2..."
    sleep 300
    
    echo "Deploying Phase 2 (+5 Pods)..."
    helm install "$HELM_RELEASE-p2" "$CHART_DIR" -n "$NAMESPACE" --create-namespace \
      --set replicaCount=5 --set namePrefix="fio-t11-p2" \
      --set-file fioJob.content="$CHART_DIR/jobs/tests/test11_burst_read_phase2.fio"
    ;;

  *test_example*)
    echo "Running Local Example: Phase 1 (10 Pods)..."
    helm install "$HELM_RELEASE-p1" "$CHART_DIR" -n "$NAMESPACE" --create-namespace \
      --set replicaCount=10 --set namePrefix="fio-example-p1" \
      --set namespace="$NAMESPACE" \
      --set pvc.size=2Gi \
      --set resources.requests.cpu=100m \
      --set resources.requests.memory=128Mi \
      --set-file fioJob.content="$CHART_DIR/jobs/tests/test_example_phase1.fio"
    
    echo "Waiting 2 minutes for Phase 2..."
    sleep 120
    
    echo "Running Local Example: Phase 2 (+1 Pod)..."
    helm install "$HELM_RELEASE-p2" "$CHART_DIR" -n "$NAMESPACE" --create-namespace \
      --set replicaCount=1 --set namePrefix="fio-example-p2" \
      --set namespace="$NAMESPACE" \
      --set pvc.size=2Gi \
      --set resources.requests.cpu=100m \
      --set resources.requests.memory=128Mi \
      --set-file fioJob.content="$CHART_DIR/jobs/tests/test_example_phase2.fio"
    ;;

  *gradual_scale*)
    echo "Deploying initial 10 Pods..."
    helm install "$HELM_RELEASE" "$CHART_DIR" -n "$NAMESPACE" --create-namespace \
      --set replicaCount=10 --set namePrefix="fio-scale" \
      --set-file fioJob.content="$CHART_DIR/jobs/tests/$TEST_ID.fio"
    
    # Scale up by 5 pods every minute for 30 minutes (up to 160 pods? 10 + 5*30 = 160)
    for i in {1..30}; do
       sleep 60
       NEW_REPLICAS=$((10 + i * 5))
       echo "Scaling to $NEW_REPLICAS Pods..."
       helm upgrade "$HELM_RELEASE" "$CHART_DIR" -n "$NAMESPACE" \
         --set replicaCount=$NEW_REPLICAS --set namePrefix="fio-scale" \
         --reuse-values
    done
    ;;

  *test17_mixed_workload*)
    echo "Deploying Mixed Workloads (40 Pods total across 4 releases)..."
    helm install "$HELM_RELEASE-32k" "$CHART_DIR" -n "$NAMESPACE" --create-namespace --set replicaCount=10 --set namePrefix="fio-t17-32k" --set-file fioJob.content="$CHART_DIR/jobs/tests/test17_mixed_workload_32k.fio"
    helm install "$HELM_RELEASE-64k" "$CHART_DIR" -n "$NAMESPACE" --create-namespace --set replicaCount=10 --set namePrefix="fio-t17-64k" --set-file fioJob.content="$CHART_DIR/jobs/tests/test17_mixed_workload_64k.fio"
    helm install "$HELM_RELEASE-256k" "$CHART_DIR" -n "$NAMESPACE" --create-namespace --set replicaCount=10 --set namePrefix="fio-t17-256k" --set-file fioJob.content="$CHART_DIR/jobs/tests/test17_mixed_workload_256k.fio"
    helm install "$HELM_RELEASE-512k" "$CHART_DIR" -n "$NAMESPACE" --create-namespace --set replicaCount=10 --set namePrefix="fio-t17-512k" --set-file fioJob.content="$CHART_DIR/jobs/tests/test17_mixed_workload_512k.fio"
    ;;

  *)
    # Default behavior (Test 1-6)
    helm install "$HELM_RELEASE" "$CHART_DIR" -n "$NAMESPACE" --create-namespace \
      --set replicaCount=$REPLICAS \
      --set namePrefix="fio-$TEST_ID" \
      --set-file fioJob.content="$CHART_DIR/jobs/tests/$TEST_ID.fio"
    ;;
esac

echo ""
echo "Deployment triggered successfully for $TEST_ID."
echo "Check pods with: kubectl get pods -n $NAMESPACE"
