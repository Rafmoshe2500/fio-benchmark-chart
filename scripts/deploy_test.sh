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

HELM_RELEASE="fio-$TEST_ID"
HELM_RELEASE=$(echo "$HELM_RELEASE" | tr '_' '-' | cut -c 1-45)

# פונקציית חלוקה התומכת בניתוב לפי סוג הבדיקה (Throughput / IOPS) וביחס של 65/35
deploy_split() {
  local phase_suffix=$1
  local job_file=$2
  local total_pods=$3
  local mode=${4:-both} # "both" או "100g_only"

  local rep_100g=0
  local rep_10g=0

  if [ "$mode" == "100g_only" ]; then
    rep_100g=$total_pods
    rep_10g=0
  else
    # מצב both: חלוקה של 65% ל-100G ו-35% ל-10G
    rep_100g=$(( total_pods * 65 / 100 ))
    rep_10g=$(( total_pods - rep_100g ))
    
    # בבדיקות Max IOPS של פוד בודד, נרים פוד אחד בכל סביבה לצורך השוואה
    if [ $total_pods -eq 1 ]; then
      rep_100g=1
      rep_10g=1
    fi
  fi

  local rel_name="${HELM_RELEASE}${phase_suffix}"

  if [ $rep_10g -gt 0 ]; then
    echo "--> Deploying $rep_10g Pods to 10G Nodes..."
    helm install "${rel_name}-10g" "$CHART_DIR" -n "$NAMESPACE" --create-namespace \
      --set replicaCount=$rep_10g \
      --set nodeSelector.netclass=slow10g \
      --set pvc.storageClassName=csi-vast-sc-10 \
      --set namePrefix="${rel_name}-10g" \
      --set-file fioJob.content="$job_file"
  fi

  if [ $rep_100g -gt 0 ]; then
    echo "--> Deploying $rep_100g Pods to 100G Nodes..."
    helm install "${rel_name}-100g" "$CHART_DIR" -n "$NAMESPACE" --create-namespace \
      --set replicaCount=$rep_100g \
      --set nodeSelector.netclass=fast100g \
      --set pvc.storageClassName=csi-vast-sc-100 \
      --set namePrefix="${rel_name}-100g" \
      --set-file fioJob.content="$job_file"
  fi
}

case $TEST_ID in
  
  # בדיקות פוד בודד (Max IOPS) - ירוץ על שתי הסביבות במקביל (פוד 1 לכל סוג רשת)
  *1pod*) 
    deploy_split "" "$CHART_DIR/jobs/tests/$TEST_ID.fio" 1 "both"
    ;;
  
  # בדיקות 10 פודים מוכוונות Throughput (טסטים 5 ו-6)
  *test5*|*test6*)
    deploy_split "" "$CHART_DIR/jobs/tests/$TEST_ID.fio" 10 "100g_only"
    ;;

  # בדיקת Burst Write (מוכוונת Throughput 3GB -> 10GB)
  *test10_burst_write*)
    echo "Deploying Phase 1 (5 Pods on 100G)..."
    deploy_split "-p1" "$CHART_DIR/jobs/tests/test10_burst_write_phase1.fio" 5 "100g_only"
    
    echo "Waiting 5 minutes..."
    sleep 300
    
    echo "Deploying Phase 2 (+5 Pods on 100G)..."
    deploy_split "-p2" "$CHART_DIR/jobs/tests/test10_burst_write_phase2.fio" 5 "100g_only"
    ;;
    
  # בדיקת Burst Read (מוכוונת Throughput 5GB -> 10GB)
  *test11_burst_read*)
    echo "Deploying Phase 1 (20 Pods on 100G)..."
    deploy_split "-p1" "$CHART_DIR/jobs/tests/test11_burst_read_phase1.fio" 20 "100g_only"
    
    echo "Waiting 5 minutes..."
    sleep 300
    
    echo "Deploying Phase 2 (+5 Pods on 100G)..."
    deploy_split "-p2" "$CHART_DIR/jobs/tests/test11_burst_read_phase2.fio" 5 "100g_only"
    ;;

  # בדיקות גדילה הדרגתית (Gradual Scale)
  *gradual_scale*)
    # אם שם הבדיקה מכיל בלוקים גדולים (1m או 512k), זו בדיקת Throughput שתרוץ רק על 100G
    MODE="both"
    if [[ "$TEST_ID" == *"1m"* || "$TEST_ID" == *"512k"* ]]; then
        MODE="100g_only"
    fi
    
    echo "Deploying Initial Scale (10 Pods)..."
    deploy_split "" "$CHART_DIR/jobs/tests/$TEST_ID.fio" 10 "$MODE"
    
    for i in {1..30}; do
       sleep 60
       TOTAL_NOW=$(( 10 + i * 5 ))
       
       if [ "$MODE" == "100g_only" ]; then
         REP_100G=$TOTAL_NOW
         REP_10G=0
         echo "Minute $i: Scaling to total $TOTAL_NOW Pods (All on 100G)..."
       else
         # יחס של 65/35 לטובת 100G
         REP_100G=$(( TOTAL_NOW * 65 / 100 ))
         REP_10G=$(( TOTAL_NOW - REP_100G ))
         echo "Minute $i: Scaling to total $TOTAL_NOW Pods ($REP_10G on 10G, $REP_100G on 100G)..."
       fi
       
       if [ $REP_10G -gt 0 ]; then
         helm upgrade "${HELM_RELEASE}-10g" "$CHART_DIR" -n "$NAMESPACE" \
           --set replicaCount=$REP_10G --set namePrefix="${HELM_RELEASE}-10g" --reuse-values || true
       fi
       if [ $REP_100G -gt 0 ]; then
         helm upgrade "${HELM_RELEASE}-100g" "$CHART_DIR" -n "$NAMESPACE" \
           --set replicaCount=$REP_100G --set namePrefix="${HELM_RELEASE}-100g" --reuse-values || true
       fi
    done
    ;;

  # בדיקת עומס מעורב - ממוקדת ב-IOPS (גדלים שונים)
  *test17_mixed_workload*)
    echo "Deploying Mixed Workloads (10 Pods per block size, spread 65/35)..."
    deploy_split "-32k" "$CHART_DIR/jobs/tests/test17_mixed_workload_32k.fio" 10 "both"
    deploy_split "-64k" "$CHART_DIR/jobs/tests/test17_mixed_workload_64k.fio" 10 "both"
    deploy_split "-256k" "$CHART_DIR/jobs/tests/test17_mixed_workload_256k.fio" 10 "both"
    deploy_split "-512k" "$CHART_DIR/jobs/tests/test17_mixed_workload_512k.fio" 10 "both"
    ;;

  # ברירת מחדל לטסטים 1-4 (בדיקות IOPS נקיות של 30k/50k)
  *)
    deploy_split "" "$CHART_DIR/jobs/tests/$TEST_ID.fio" 10 "both"
    ;;
esac

echo ""
echo "Deployment triggered successfully for $TEST_ID."
echo "Check pods with: kubectl get pods -n $NAMESPACE"