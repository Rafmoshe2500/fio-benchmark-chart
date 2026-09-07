#!/usr/bin/env bash
#
# nifi-nfs-loadtest.sh -- stand up Apache NiFi 2.x on Kubernetes with every
# repository on an NFS-backed StorageClass, start a synthetic flow, and
# measure what it does to the array.
#
#   ./nifi-nfs-loadtest.sh all        preflight -> smoke -> deploy -> flow -> stats
#   ./nifi-nfs-loadtest.sh preflight  check StorageClass / SCC / capacity first
#   ./nifi-nfs-loadtest.sh smoke      prove uid 1000 can write, before deploying
#   ./nifi-nfs-loadtest.sh deploy     create ns, SA, PVCs, StatefulSet
#   ./nifi-nfs-loadtest.sh flow       build + start the load flow on all nodes
#   ./nifi-nfs-loadtest.sh stats      live throughput + repo utilisation
#   ./nifi-nfs-loadtest.sh stop|start pause / resume the load
#   ./nifi-nfs-loadtest.sh clear      wipe the flow (keeps pods + data)
#   ./nifi-nfs-loadtest.sh ui [n]     port-forward node n's UI
#   ./nifi-nfs-loadtest.sh logs [n]   tail node n's nifi-app.log
#   ./nifi-nfs-loadtest.sh teardown   delete everything including PVCs
#
# Everything below is overridable from the environment, e.g.
#   STORAGE_CLASS=vast-nfs REPLICAS=4 PROFILE=bigfile ./nifi-nfs-loadtest.sh deploy
#
set -euo pipefail

# ============================== TUNABLES ==============================

NS="${NS:-nifi-loadtest}"
STORAGE_CLASS="${STORAGE_CLASS:-nfs-client}"
NIFI_IMAGE="${NIFI_IMAGE:-apache/nifi:2.11.0}"
REPLICAS="${REPLICAS:-3}"

# openshift | k8s | auto. On OpenShift a dedicated ServiceAccount is created
# and the pod security context is written to satisfy the nonroot-v2 SCC.
PLATFORM="${PLATFORM:-auto}"
SA_NAME="${SA_NAME:-nifi-loadtest}"
SCC="${SCC:-nonroot-v2}"

# kubelet applies fsGroup with a recursive chown on the mounted volume. On an
# NFS export with root_squash that chown fails and the pod never starts. Set
# FSGROUP= (empty) to omit it and let the export's own permissions govern.
FSGROUP="${FSGROUP-1000}"   # note: ${VAR-default}, so FSGROUP= is honoured

CONF_SIZE="${CONF_SIZE:-1Gi}"
FLOWFILE_REPO_SIZE="${FLOWFILE_REPO_SIZE:-10Gi}"
CONTENT_REPO_SIZE="${CONTENT_REPO_SIZE:-50Gi}"
PROVENANCE_REPO_SIZE="${PROVENANCE_REPO_SIZE:-20Gi}"
DATA_SIZE="${DATA_SIZE:-100Gi}"          # shared RWX write target

HEAP="${HEAP:-4g}"
CPU_REQ="${CPU_REQ:-2}"; CPU_LIM="${CPU_LIM:-4}"
MEM_REQ="${MEM_REQ:-6Gi}"; MEM_LIM="${MEM_LIM:-8Gi}"

# Load profile:
#   smallfile : 4 KB x 200/batch  -> flowfile-repo WAL + metadata storm
#   bigfile   : 16 MB x 1         -> sequential content-repo streaming
#   churn     : 512 KB, 3 rewrites-> content-claim churn + provenance heavy
PROFILE="${PROFILE:-smallfile}"

FILE_SIZE="${FILE_SIZE:-}"
BATCH_SIZE="${BATCH_SIZE:-}"
CONCURRENT="${CONCURRENT:-}"
REWRITES="${REWRITES:-}"
SCHEDULE="${SCHEDULE:-0 sec}"            # 0 sec = run flat out

# NiFi-side storage knobs that materially change the I/O pattern.
ARCHIVE_ENABLED="${ARCHIVE_ENABLED:-false}"
CHECKPOINT_INTERVAL="${CHECKPOINT_INTERVAL:-20 secs}"
MAX_APPENDABLE_SIZE="${MAX_APPENDABLE_SIZE:-1 MB}"
ALWAYS_SYNC="${ALWAYS_SYNC:-false}"      # true = fsync every write. NFS killer.

# Write the output to the shared RWX export as well as through the repos.
WRITE_OUTPUT="${WRITE_OUTPUT:-true}"

BP_OBJECTS="${BP_OBJECTS:-20000}"
BP_SIZE="${BP_SIZE:-4 GB}"

NIFI_USER="${NIFI_USER:-admin}"
NIFI_PASS="${NIFI_PASS:-loadtestAdminPass123}"    # >= 12 chars, required
SENSITIVE_KEY="${SENSITIVE_KEY:-loadtestSensitivePropsKey123}"

PORT_BASE="${PORT_BASE:-18443}"
API_WAIT="${API_WAIT:-420}"   # seconds to wait for each node's REST API
HELPER="${HELPER:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/nififlow.py}"

# ======================================================================

log()  { printf '\033[1;34m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }

for t in kubectl python3 curl; do
  command -v "$t" >/dev/null 2>&1 || die "missing required tool: $t"
done
[[ -f "$HELPER" ]] || die "nififlow.py not found at $HELPER (keep both files together)"

# ---- platform detection ----
if [[ "$PLATFORM" == "auto" ]]; then
  if kubectl api-resources --api-group=security.openshift.io 2>/dev/null | grep -q securitycontextconstraints; then
    PLATFORM=openshift
  else
    PLATFORM=k8s
  fi
fi
[[ "$PLATFORM" =~ ^(openshift|k8s)$ ]] || die "PLATFORM must be openshift, k8s or auto"
OC="kubectl"; command -v oc >/dev/null 2>&1 && OC="oc"

case "$PROFILE" in
  smallfile) : "${FILE_SIZE:=4 KB}";   : "${BATCH_SIZE:=200}"; : "${CONCURRENT:=8}"; : "${REWRITES:=0}" ;;
  bigfile)   : "${FILE_SIZE:=16 MB}";  : "${BATCH_SIZE:=1}";   : "${CONCURRENT:=4}"; : "${REWRITES:=0}" ;;
  churn)     : "${FILE_SIZE:=512 KB}"; : "${BATCH_SIZE:=20}";  : "${CONCURRENT:=6}"; : "${REWRITES:=3}" ;;
  *) die "unknown PROFILE '$PROFILE' (smallfile|bigfile|churn)" ;;
esac

fsgroup_line() {
  [[ -n "$FSGROUP" ]] && printf '        fsGroup: %s' "$FSGROUP"
  return 0
}

proxy_hosts() {
  local out="" i
  for ((i=0; i<REPLICAS; i++)); do out+="127.0.0.1:$((PORT_BASE+i)),localhost:$((PORT_BASE+i)),"; done
  echo "${out%,}"
}

# ============================== MANIFESTS =============================

render() {
cat <<YAML
apiVersion: v1
kind: Namespace
metadata: { name: ${NS} }
---
# Dedicated SA so an SCC can be bound to it on OpenShift without
# touching the namespace default service account.
apiVersion: v1
kind: ServiceAccount
metadata: { name: ${SA_NAME}, namespace: ${NS} }
---
# Shared RWX target: all nodes write here at once. This is the
# concurrent-writer test against a single NFS export.
apiVersion: v1
kind: PersistentVolumeClaim
metadata: { name: nifi-data, namespace: ${NS} }
spec:
  accessModes: ["ReadWriteMany"]
  storageClassName: ${STORAGE_CLASS}
  resources: { requests: { storage: ${DATA_SIZE} } }
---
apiVersion: v1
kind: ConfigMap
metadata: { name: nifi-tuning, namespace: ${NS} }
data:
  tune.sh: |
    #!/bin/bash
    # Runs after conf/ is seeded, before NiFi starts.
    P="\${NIFI_HOME}/conf/nifi.properties"
    sp() { grep -q "^\$1=" "\$P" && sed -i "s|^\$1=.*|\$1=\$2|" "\$P" || echo "\$1=\$2" >> "\$P"; }

    sp nifi.content.repository.archive.enabled "${ARCHIVE_ENABLED}"
    sp nifi.flowfile.repository.checkpoint.interval "${CHECKPOINT_INTERVAL}"
    sp nifi.content.claim.max.appendable.size "${MAX_APPENDABLE_SIZE}"
    sp nifi.content.repository.always.sync "${ALWAYS_SYNC}"
    sp nifi.flowfile.repository.always.sync "${ALWAYS_SYNC}"
    sp nifi.provenance.repository.always.sync "${ALWAYS_SYNC}"

    echo "=== repository configuration in effect ==="
    grep -E '^nifi\.(content|flowfile|provenance)\.' "\$P" | grep -vE '^\s*#'
---
apiVersion: v1
kind: Service
metadata: { name: nifi, namespace: ${NS} }
spec:
  clusterIP: None
  selector: { app: nifi-loadtest }
  ports: [{ name: https, port: 8443 }]
---
apiVersion: apps/v1
kind: StatefulSet
metadata: { name: nifi, namespace: ${NS} }
spec:
  serviceName: nifi
  replicas: ${REPLICAS}
  podManagementPolicy: Parallel
  selector:
    matchLabels: { app: nifi-loadtest }
  template:
    metadata:
      labels: { app: nifi-loadtest }
    spec:
      serviceAccountName: ${SA_NAME}
      # uid 1000 is what the apache/nifi image is built around: /opt/nifi is
      # owned by nifi:nifi (1000:1000). OpenShift's default restricted-v2 SCC
      # forces a random uid from the namespace range instead, which the image
      # cannot write to -- hence the nonroot-v2 binding printed by 'deploy'.
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        runAsGroup: 1000
$(fsgroup_line)
        seccompProfile: { type: RuntimeDefault }
      terminationGracePeriodSeconds: 90
      initContainers:
        # conf/ lives on NFS so the flow survives a pod restart, but it
        # starts empty -- seed it from the image on first boot only.
        - name: seed-conf
          image: ${NIFI_IMAGE}
          securityContext:
            allowPrivilegeEscalation: false
            capabilities: { drop: ["ALL"] }
          command: ["bash","-c"]
          args:
            - |
              if [ ! -f /seed/nifi.properties ]; then
                echo "seeding conf/ from image"
                cp -a /opt/nifi/nifi-current/conf/. /seed/
              else
                echo "conf/ already seeded, leaving it alone"
              fi
          volumeMounts:
            - { name: conf, mountPath: /seed }
      containers:
        - name: nifi
          image: ${NIFI_IMAGE}
          imagePullPolicy: IfNotPresent
          securityContext:
            allowPrivilegeEscalation: false
            capabilities: { drop: ["ALL"] }
          command: ["/bin/bash","-c"]
          args: ["bash /tuning/tune.sh && exec ../scripts/start.sh"]
          ports: [{ containerPort: 8443, name: https }]
          env:
            - { name: NIFI_WEB_HTTPS_PORT, value: "8443" }
            # Without this the image binds nifi.web.https.host to $HOSTNAME,
            # i.e. the pod IP only. kubectl port-forward targets 127.0.0.1
            # inside the pod's netns, so it would get connection refused.
            - { name: NIFI_WEB_HTTPS_HOST, value: "0.0.0.0" }
            - { name: NIFI_WEB_PROXY_HOST, value: "$(proxy_hosts)" }
            - { name: SINGLE_USER_CREDENTIALS_USERNAME, value: "${NIFI_USER}" }
            - { name: SINGLE_USER_CREDENTIALS_PASSWORD, value: "${NIFI_PASS}" }
            - { name: NIFI_SENSITIVE_PROPS_KEY, value: "${SENSITIVE_KEY}" }
            - { name: NIFI_JVM_HEAP_INIT, value: "${HEAP}" }
            - { name: NIFI_JVM_HEAP_MAX, value: "${HEAP}" }
          resources:
            requests: { cpu: "${CPU_REQ}", memory: "${MEM_REQ}" }
            limits:   { cpu: "${CPU_LIM}", memory: "${MEM_LIM}" }
          volumeMounts:
            - { name: tuning,          mountPath: /tuning }
            - { name: conf,            mountPath: /opt/nifi/nifi-current/conf }
            - { name: flowfile-repo,   mountPath: /opt/nifi/nifi-current/flowfile_repository }
            - { name: content-repo,    mountPath: /opt/nifi/nifi-current/content_repository }
            - { name: provenance-repo, mountPath: /opt/nifi/nifi-current/provenance_repository }
            - { name: nifi-data,       mountPath: /data }
          startupProbe:
            tcpSocket: { port: 8443 }
            failureThreshold: 90
            periodSeconds: 10
          livenessProbe:
            tcpSocket: { port: 8443 }
            initialDelaySeconds: 180
            periodSeconds: 30
            failureThreshold: 6
      volumes:
        - name: tuning
          configMap: { name: nifi-tuning, defaultMode: 0755 }
        - name: nifi-data
          persistentVolumeClaim: { claimName: nifi-data }
  volumeClaimTemplates:
    - metadata: { name: conf }
      spec:
        accessModes: ["ReadWriteOnce"]
        storageClassName: ${STORAGE_CLASS}
        resources: { requests: { storage: ${CONF_SIZE} } }
    - metadata: { name: flowfile-repo }
      spec:
        accessModes: ["ReadWriteOnce"]
        storageClassName: ${STORAGE_CLASS}
        resources: { requests: { storage: ${FLOWFILE_REPO_SIZE} } }
    - metadata: { name: content-repo }
      spec:
        accessModes: ["ReadWriteOnce"]
        storageClassName: ${STORAGE_CLASS}
        resources: { requests: { storage: ${CONTENT_REPO_SIZE} } }
    - metadata: { name: provenance-repo }
      spec:
        accessModes: ["ReadWriteOnce"]
        storageClassName: ${STORAGE_CLASS}
        resources: { requests: { storage: ${PROVENANCE_REPO_SIZE} } }
YAML
}

# ============================ PORT FORWARDS ===========================

PF_PIDS=()
cleanup() { for p in "${PF_PIDS[@]:-}"; do kill "$p" 2>/dev/null || true; done; PF_PIDS=(); }
trap cleanup EXIT INT TERM

forward_all() {
  local i port
  for ((i=0; i<REPLICAS; i++)); do
    port=$((PORT_BASE+i))
    kubectl -n "$NS" port-forward "pod/nifi-${i}" "${port}:8443" >/dev/null 2>&1 &
    PF_PIDS+=($!)
  done
  # give the tunnels a moment, then confirm each one answers
  local ok=0
  for _ in $(seq 1 30); do
    sleep 2; ok=0
    for ((i=0; i<REPLICAS; i++)); do
      if curl -sk --noproxy '*' --max-time 3 \
           "https://127.0.0.1:$((PORT_BASE+i))/nifi-api/access" >/dev/null 2>&1; then
        ok=$((ok+1))
      fi
    done
    [[ $ok -eq $REPLICAS ]] && return 0
  done
  warn "only ${ok}/${REPLICAS} nodes answering over the port-forward"
  warn "NiFi opens its port before the REST API is ready; the next step retries."
}

nfy() { # nfy <node-index> <action> [extra args...]
  local idx="$1"; shift
  python3 "$HELPER" "$1" \
    --url "https://127.0.0.1:$((PORT_BASE+idx))" \
    --user "$NIFI_USER" --password "$NIFI_PASS" \
    --label "nifi-${idx}" "${@:2}"
}

# ============================== COMMANDS ==============================

cmd_preflight() {
  log "platform: ${PLATFORM}"
  echo
  echo "--- storage classes ---"
  kubectl get sc || warn "cannot list StorageClasses"
  echo
  echo "--- requested: ${STORAGE_CLASS} ---"
  if kubectl get sc "$STORAGE_CLASS" >/dev/null 2>&1; then
    kubectl get sc "$STORAGE_CLASS" -o custom-columns=NAME:.metadata.name,PROVISIONER:.provisioner
  else
    warn "StorageClass '${STORAGE_CLASS}' does not exist -- PVCs will hang Pending"
  fi
  if [[ "$PLATFORM" == "openshift" ]]; then
    echo
    echo "--- SCC ---"
    if kubectl get scc "$SCC" >/dev/null 2>&1; then
      echo "  ${SCC} exists"
    else
      warn "SCC '${SCC}' not found; available:"
      kubectl get scc -o name 2>/dev/null | sed 's/^/    /'
    fi
    if kubectl auth can-i create rolebindings -n "$NS" >/dev/null 2>&1 \
       || kubectl auth can-i '*' '*' >/dev/null 2>&1; then
      echo "  you appear able to bind an SCC"
    else
      warn "you may lack rights to bind an SCC -- ask a cluster admin to run:"
      echo "    oc adm policy add-scc-to-user ${SCC} -z ${SA_NAME} -n ${NS}"
    fi
    if kubectl get ns "$NS" >/dev/null 2>&1; then
      echo -n "  namespace uid range: "
      kubectl get ns "$NS" -o jsonpath='{.metadata.annotations.openshift\.io/sa\.scc\.uid-range}' 2>/dev/null
      echo " (irrelevant once ${SCC} is bound; we pin uid 1000)"
    fi
  fi
  echo
  echo "--- image ---"
  echo "  ${NIFI_IMAGE}  (verify this tag exists and is reachable from the cluster)"
  echo
  echo "--- capacity needed ---"
  echo "  ${REPLICAS} pods x (${CPU_REQ} cpu, ${MEM_REQ}) requested"
  echo "  PVCs: ${REPLICAS} x (${CONF_SIZE} + ${FLOWFILE_REPO_SIZE} + ${CONTENT_REPO_SIZE} + ${PROVENANCE_REPO_SIZE}) RWO"
  [[ "$WRITE_OUTPUT" == "true" ]] && echo "        1 x ${DATA_SIZE} RWX (needs ReadWriteMany support)"
  echo
  log "NFS export must permit writes by uid 1000 -- that is the most common failure"
}

bind_scc() {
  [[ "$PLATFORM" == "openshift" ]] || return 0
  log "binding SCC '${SCC}' to serviceaccount ${SA_NAME}"
  if $OC adm policy add-scc-to-user "$SCC" -z "$SA_NAME" -n "$NS" 2>/dev/null; then
    log "SCC bound"
  else
    warn "could not bind the SCC yourself. Have a cluster admin run:"
    warn "    oc adm policy add-scc-to-user ${SCC} -z ${SA_NAME} -n ${NS}"
    warn "without it the pods will be rejected at admission:"
    warn "    'unable to validate against any security context constraint'"
  fi
}

ensure_ns_sa() {
  kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl -n "$NS" create serviceaccount "$SA_NAME" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
}

cmd_smoke() {
  log "smoke test: SCC admission + uid 1000 write access on ${STORAGE_CLASS}"
  kubectl get sc "$STORAGE_CLASS" >/dev/null 2>&1 \
    || die "StorageClass '${STORAGE_CLASS}' does not exist. Candidates:
$(kubectl get sc -o custom-columns=NAME:.metadata.name,PROV:.provisioner --no-headers 2>/dev/null | sed 's/^/    /')"

  ensure_ns_sa
  bind_scc

  # Each object gets its own heredoc. Do not try to splice YAML fragments
  # together from shell strings; that is how newlines get eaten.
  kubectl apply -f - >/dev/null <<YAML
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: smoke-rwo
  namespace: ${NS}
spec:
  accessModes: ["ReadWriteOnce"]
  storageClassName: ${STORAGE_CLASS}
  resources:
    requests:
      storage: 1Gi
YAML

  if [[ "$WRITE_OUTPUT" == "true" ]]; then
    kubectl apply -f - >/dev/null <<YAML
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: smoke-rwx
  namespace: ${NS}
spec:
  accessModes: ["ReadWriteMany"]
  storageClassName: ${STORAGE_CLASS}
  resources:
    requests:
      storage: 1Gi
YAML
  fi

  # Build the probe pod in a temp file so the conditional RWX parts are
  # appended as real lines rather than escaped strings.
  local pod; pod="$(mktemp)"
  cat > "$pod" <<YAML
apiVersion: v1
kind: Pod
metadata:
  name: nifi-smoke
  namespace: ${NS}
spec:
  serviceAccountName: ${SA_NAME}
  restartPolicy: Never
  securityContext:
    runAsNonRoot: true
    runAsUser: 1000
    runAsGroup: 1000
$(fsgroup_line | sed 's/^    //')
    seccompProfile:
      type: RuntimeDefault
  containers:
    - name: probe
      image: ${NIFI_IMAGE}
      securityContext:
        allowPrivilegeEscalation: false
        capabilities:
          drop: ["ALL"]
      command: ["/bin/bash","-c"]
      args:
        - |
          rc=0
          echo "running as uid \$(id -u) gid \$(id -g)"
          if echo rwo > /rwo/probe && [ "\$(cat /rwo/probe)" = rwo ]; then
            echo "RWO  write OK"
          else
            echo "RWO  write FAILED"; rc=1
          fi
YAML

  if [[ "$WRITE_OUTPUT" == "true" ]]; then
    cat >> "$pod" <<'YAML'
          if echo rwx > /rwx/probe && [ "$(cat /rwx/probe)" = rwx ]; then
            echo "RWX  write OK"
          else
            echo "RWX  write FAILED"; rc=1
          fi
YAML
  fi

  cat >> "$pod" <<'YAML'
          # NiFi's flowfile repository depends on fsync. Prove it works
          # on this mount and show what it costs.
          dd if=/dev/zero of=/rwo/fsync.bin bs=1M count=16 oflag=dsync 2>&1 | tail -1
          rm -f /rwo/probe /rwo/fsync.bin
          exit $rc
      volumeMounts:
        - name: rwo
          mountPath: /rwo
YAML

  if [[ "$WRITE_OUTPUT" == "true" ]]; then
    cat >> "$pod" <<'YAML'
        - name: rwx
          mountPath: /rwx
YAML
  fi

  cat >> "$pod" <<'YAML'
  volumes:
    - name: rwo
      persistentVolumeClaim:
        claimName: smoke-rwo
YAML

  if [[ "$WRITE_OUTPUT" == "true" ]]; then
    cat >> "$pod" <<'YAML'
    - name: rwx
      persistentVolumeClaim:
        claimName: smoke-rwx
YAML
  fi

  if ! kubectl apply -f "$pod" >/dev/null; then
    warn "probe pod rejected. Rendered manifest:"; cat "$pod" >&2; rm -f "$pod"
    die "smoke test could not start"
  fi
  rm -f "$pod"

  log "waiting for the probe pod (first run pulls the NiFi image, be patient)..."
  local phase="" reason="" i last=""
  for i in $(seq 1 100); do
    phase=$(kubectl -n "$NS" get pod nifi-smoke -o jsonpath='{.status.phase}' 2>/dev/null || echo "")
    [[ "$phase" == "Succeeded" || "$phase" == "Failed" ]] && break
    reason=$(kubectl -n "$NS" get pod nifi-smoke \
      -o jsonpath='{.status.containerStatuses[0].state.waiting.reason}' 2>/dev/null || echo "")
    if [[ -n "$reason" && "$reason" != "$last" ]]; then log "  ${reason}"; last="$reason"; fi
    sleep 3
  done

  echo
  if [[ -z "$phase" || "$phase" == "Pending" ]]; then
    warn "probe pod never ran. Events:"
    kubectl -n "$NS" describe pod nifi-smoke 2>/dev/null | sed -n '/Events:/,$p' | head -20
    echo; kubectl -n "$NS" get pvc 2>/dev/null
  else
    kubectl -n "$NS" logs nifi-smoke 2>/dev/null | sed 's/^/    /'
  fi
  echo

  kubectl -n "$NS" delete pod nifi-smoke --wait=false >/dev/null 2>&1 || true
  kubectl -n "$NS" delete pvc smoke-rwo smoke-rwx --wait=false >/dev/null 2>&1 || true

  if [[ "$phase" == "Succeeded" ]]; then
    log "smoke test PASSED -- safe to run: $0 deploy"
    return 0
  fi
  die "smoke test failed. Fix the above before deploying (nothing else was created)."
}

cmd_all() {
  cmd_preflight
  echo
  cmd_smoke
  echo
  cmd_deploy
  echo
  cmd_flow
  echo
  cmd_stats
}

cmd_deploy() {
  [[ ${#NIFI_PASS} -ge 12 ]] || die "NIFI_PASS must be >= 12 chars; NiFi 2.x refuses to boot otherwise"
  log "platform=${PLATFORM} ns=${NS} sc=${STORAGE_CLASS} replicas=${REPLICAS} image=${NIFI_IMAGE}"
  log "profile=${PROFILE} size=${FILE_SIZE} batch=${BATCH_SIZE} threads=${CONCURRENT} rewrites=${REWRITES}"

  # Namespace and SA first, so the SCC can be bound before any pod is scheduled.
  ensure_ns_sa
  bind_scc

  render | kubectl apply -f -
  log "waiting for rollout (first boot on NFS can take several minutes)..."
  kubectl -n "$NS" rollout status statefulset/nifi --timeout=20m \
    || warn "not ready -- inspect with: kubectl -n $NS logs nifi-0 -c nifi"
  kubectl -n "$NS" get pods -o wide
  kubectl -n "$NS" get pvc
  log "next: $0 flow"
}

cmd_flow() {
  forward_all
  local i out_dir=""
  [[ "$WRITE_OUTPUT" == "true" ]] && out_dir='/data/out/${hostname()}'
  for ((i=0; i<REPLICAS; i++)); do
    log "waiting for nifi-${i} REST API"
    nfy "$i" ping --wait "${API_WAIT}" || die "nifi-${i} never became usable"
    log "building flow on nifi-${i}"
    nfy "$i" clear >/dev/null 2>&1 || true
    nfy "$i" build \
      --file-size "$FILE_SIZE" --batch "$BATCH_SIZE" --threads "$CONCURRENT" \
      --rewrites "$REWRITES" --schedule "$SCHEDULE" \
      --bp-objects "$BP_OBJECTS" --bp-size "$BP_SIZE" \
      --output-dir "$out_dir" \
      || die "flow build failed on nifi-${i}"
    nfy "$i" state --state RUNNING
    log "nifi-${i}: RUNNING"
  done
  log "load is live. watch it with: $0 stats"
}

cmd_state() {
  forward_all
  local i
  for ((i=0; i<REPLICAS; i++)); do nfy "$i" state --state "$1"; log "nifi-${i}: $1"; done
}

cmd_clear() { forward_all; local i; for ((i=0;i<REPLICAS;i++)); do nfy "$i" clear; log "nifi-${i}: flow cleared"; done; }

cmd_stats() {
  forward_all
  log "sampling every 10s -- ctrl-c to stop"
  while true; do
    echo "===== $(date '+%Y-%m-%d %H:%M:%S') ====="
    local i
    for ((i=0; i<REPLICAS; i++)); do
      nfy "$i" stats 2>/dev/null || warn "nifi-${i}: no stats"
    done
    sleep 10
  done
}

cmd_ui() {
  local idx="${1:-0}" port=$((PORT_BASE + ${1:-0}))
  log "UI: https://127.0.0.1:${port}/nifi   user=${NIFI_USER}  pass=${NIFI_PASS}"
  log "self-signed cert -- accept the browser warning"
  trap - EXIT INT TERM
  kubectl -n "$NS" port-forward "pod/nifi-${idx}" "${port}:8443"
}

cmd_logs() {
  kubectl -n "$NS" exec -it "nifi-${1:-0}" -c nifi -- \
    tail -f /opt/nifi/nifi-current/logs/nifi-app.log
}

cmd_teardown() {
  read -rp "delete namespace ${NS} and ALL its PVCs? [y/N] " a
  [[ "$a" == "y" ]] || { log "aborted"; exit 0; }
  kubectl delete namespace "$NS" --wait=true
  log "gone"
}

case "${1:-}" in
  all)       cmd_all ;;
  preflight) cmd_preflight ;;
  smoke)     cmd_smoke ;;
  render)   render ;;
  deploy)   cmd_deploy ;;
  flow)     cmd_flow ;;
  start)    cmd_state RUNNING ;;
  stop)     cmd_state STOPPED ;;
  clear)    cmd_clear ;;
  stats)    cmd_stats ;;
  ui)       cmd_ui "${2:-0}" ;;
  logs)     cmd_logs "${2:-0}" ;;
  teardown) cmd_teardown ;;
  *) sed -n '3,20p' "$0"; exit 1 ;;
esac
