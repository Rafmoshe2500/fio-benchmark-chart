#!/usr/bin/env bash
#
# nifi-cluster.sh -- deploy a REAL NiFi cluster, not N standalone instances.
#
# nifi-nfs-loadtest.sh runs independent NiFi nodes. That is the right shape
# for loading a storage array: each node drives its own repositories and the
# aggregate is the array's load. It is the wrong shape for any claim about
# NiFi behaviour, because none of the cluster machinery is exercised --
# there is no coordinator, no load-balanced connection, no primary-node
# processor, no cluster state, and nothing to fail over.
#
# This mode adds the machinery so those can be tested. It is a separate
# script rather than a flag because the two answer different questions and
# should not be confused in a report.
#
#   ./nifi-cluster.sh deploy        ZooKeeper + a clustered NiFi StatefulSet
#   ./nifi-cluster.sh status        cluster membership and the coordinator
#   ./nifi-cluster.sh failover      kill the coordinator, time re-election
#   ./nifi-cluster.sh ui            port-forward node 0's UI
#   ./nifi-cluster.sh teardown      delete everything
#
set -euo pipefail

NS="${NS:-nifi-cluster}"
STORAGE_CLASS="${STORAGE_CLASS:-sc-nas-nfs3}"
NIFI_IMAGE="${NIFI_IMAGE:-apache/nifi:2.11.0}"
ZK_IMAGE="${ZK_IMAGE:-zookeeper:3.9}"
REPLICAS="${REPLICAS:-3}"

CONF_SIZE="${CONF_SIZE:-1Gi}"
FLOWFILE_REPO_SIZE="${FLOWFILE_REPO_SIZE:-10Gi}"
CONTENT_REPO_SIZE="${CONTENT_REPO_SIZE:-50Gi}"
PROVENANCE_REPO_SIZE="${PROVENANCE_REPO_SIZE:-20Gi}"
STATE_SIZE="${STATE_SIZE:-2Gi}"

HEAP="${HEAP:-4g}"
CPU_REQ="${CPU_REQ:-2}"; CPU_LIM="${CPU_LIM:-4}"
MEM_REQ="${MEM_REQ:-6Gi}"; MEM_LIM="${MEM_LIM:-8Gi}"

NIFI_USER="${NIFI_USER:-admin}"
NIFI_PASS="${NIFI_PASS:-loadtestAdminPass123}"
SENSITIVE_KEY="${SENSITIVE_KEY:-loadtestSensitivePropsKey123}"
SA_NAME="${SA_NAME:-nifi-cluster}"
SCC="${SCC:-nonroot-v2}"
PORT_BASE="${PORT_BASE:-19443}"
FSGROUP="${FSGROUP-1000}"

log()  { printf '\033[1;32m[cluster %s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }

[[ ${#NIFI_PASS} -ge 12 ]] || die "NIFI_PASS must be >= 12 chars"

fsgroup_line() { [[ -n "$FSGROUP" ]] && printf '        fsGroup: %s' "$FSGROUP"; return 0; }

zk_connect() {
  local out="" i
  for ((i=0; i<3; i++)); do out+="zookeeper-${i}.zookeeper.${NS}.svc.cluster.local:2181,"; done
  echo "${out%,}"
}

render() {
cat <<YAML
apiVersion: v1
kind: Namespace
metadata: { name: ${NS} }
---
apiVersion: v1
kind: ServiceAccount
metadata: { name: ${SA_NAME}, namespace: ${NS} }
---
# ZooKeeper is what makes this a cluster rather than a collection: it holds
# the coordinator election and the cluster-wide component state that
# primary-node processors depend on.
apiVersion: v1
kind: Service
metadata: { name: zookeeper, namespace: ${NS} }
spec:
  clusterIP: None
  selector: { app: nifi-zk }
  ports:
  - { name: client, port: 2181 }
  - { name: peer,   port: 2888 }
  - { name: leader, port: 3888 }
---
apiVersion: apps/v1
kind: StatefulSet
metadata: { name: zookeeper, namespace: ${NS} }
spec:
  serviceName: zookeeper
  replicas: 3
  podManagementPolicy: Parallel
  selector: { matchLabels: { app: nifi-zk } }
  template:
    metadata: { labels: { app: nifi-zk } }
    spec:
      serviceAccountName: ${SA_NAME}
      # The ensemble must survive losing a worker, otherwise the failover
      # test loses the quorum along with the node it meant to kill.
      affinity:
        podAntiAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
          - topologyKey: kubernetes.io/hostname
            labelSelector:
              matchLabels: { app: nifi-zk }
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        runAsGroup: 1000
$(fsgroup_line)
      containers:
      - name: zookeeper
        image: ${ZK_IMAGE}
        env:
        - { name: ZOO_STANDALONE_ENABLED, value: "false" }
        - { name: ZOO_SERVERS, value: "server.1=zookeeper-0.zookeeper.${NS}.svc.cluster.local:2888:3888;2181 server.2=zookeeper-1.zookeeper.${NS}.svc.cluster.local:2888:3888;2181 server.3=zookeeper-2.zookeeper.${NS}.svc.cluster.local:2888:3888;2181" }
        command: ["/bin/bash","-c"]
        args:
          - |
            # ZOO_MY_ID must match the ordinal; the image does not derive it.
            export ZOO_MY_ID=\$(( \${HOSTNAME##*-} + 1 ))
            echo "starting zookeeper with id \$ZOO_MY_ID"
            exec /docker-entrypoint.sh zkServer.sh start-foreground
        ports:
        - { containerPort: 2181, name: client }
        - { containerPort: 2888, name: peer }
        - { containerPort: 3888, name: leader }
        volumeMounts:
        - { name: data, mountPath: /data }
        readinessProbe:
          exec: { command: ["bash","-c","echo ruok | nc 127.0.0.1 2181 | grep imok"] }
          initialDelaySeconds: 20
          periodSeconds: 10
  volumeClaimTemplates:
  - metadata: { name: data }
    spec:
      accessModes: ["ReadWriteOnce"]
      storageClassName: ${STORAGE_CLASS}
      resources: { requests: { storage: 2Gi } }
---
apiVersion: v1
kind: Service
metadata: { name: nifi, namespace: ${NS} }
spec:
  clusterIP: None
  selector: { app: nifi-cluster }
  ports:
  - { name: https,    port: 8443 }
  - { name: cluster,  port: 11443 }
  - { name: lb,       port: 6342 }
---
apiVersion: v1
kind: ConfigMap
metadata: { name: nifi-cluster-tuning, namespace: ${NS} }
data:
  tune.sh: |
    #!/bin/bash
    P="\${NIFI_HOME}/conf/nifi.properties"
    sp() { grep -q "^\$1=" "\$P" && sed -i "s|^\$1=.*|\$1=\$2|" "\$P" || echo "\$1=\$2" >> "\$P"; }
    FQDN="\${HOSTNAME}.nifi.${NS}.svc.cluster.local"

    # This block is the whole difference from the standalone mode.
    sp nifi.cluster.is.node true
    sp nifi.cluster.node.address "\$FQDN"
    sp nifi.cluster.node.protocol.port 11443
    sp nifi.cluster.load.balance.host "\$FQDN"
    sp nifi.cluster.load.balance.port 6342
    sp nifi.cluster.flow.election.max.wait.time "1 mins"
    sp nifi.cluster.flow.election.max.candidates ${REPLICAS}
    sp nifi.zookeeper.connect.string "$(zk_connect)"
    sp nifi.zookeeper.root.node /nifi
    sp nifi.state.management.embedded.zookeeper.start false
    sp nifi.web.https.host 0.0.0.0
    sp nifi.remote.input.host "\$FQDN"

    echo "=== cluster configuration in effect ==="
    grep -E '^nifi\.(cluster|zookeeper)\.' "\$P" | grep -vE '^\s*#'
---
apiVersion: apps/v1
kind: StatefulSet
metadata: { name: nifi, namespace: ${NS} }
spec:
  serviceName: nifi
  replicas: ${REPLICAS}
  podManagementPolicy: Parallel
  selector: { matchLabels: { app: nifi-cluster } }
  template:
    metadata: { labels: { app: nifi-cluster } }
    spec:
      serviceAccountName: ${SA_NAME}
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        runAsGroup: 1000
$(fsgroup_line)
        seccompProfile: { type: RuntimeDefault }
      terminationGracePeriodSeconds: 120
      # A cluster whose nodes share a worker cannot demonstrate failover:
      # losing that worker loses the quorum along with the node.
      affinity:
        podAntiAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
          - topologyKey: kubernetes.io/hostname
            labelSelector:
              matchLabels: { app: nifi-cluster }
      initContainers:
      - name: seed-conf
        image: ${NIFI_IMAGE}
        command: ["bash","-c"]
        args:
          - |
            if [ ! -f /seed/nifi.properties ]; then
              cp -a /opt/nifi/nifi-current/conf/. /seed/
            fi
        volumeMounts: [{ name: conf, mountPath: /seed }]
      containers:
      - name: nifi
        image: ${NIFI_IMAGE}
        command: ["/bin/bash","-c"]
        args: ["bash /tuning/tune.sh && exec ../scripts/start.sh"]
        ports:
        - { containerPort: 8443,  name: https }
        - { containerPort: 11443, name: cluster }
        - { containerPort: 6342,  name: lb }
        env:
        - { name: NIFI_WEB_HTTPS_PORT, value: "8443" }
        - { name: NIFI_WEB_HTTPS_HOST, value: "0.0.0.0" }
        - { name: SINGLE_USER_CREDENTIALS_USERNAME, value: "${NIFI_USER}" }
        - { name: SINGLE_USER_CREDENTIALS_PASSWORD, value: "${NIFI_PASS}" }
        - { name: NIFI_SENSITIVE_PROPS_KEY, value: "${SENSITIVE_KEY}" }
        - { name: NIFI_JVM_HEAP_INIT, value: "${HEAP}" }
        - { name: NIFI_JVM_HEAP_MAX,  value: "${HEAP}" }
        resources:
          requests: { cpu: "${CPU_REQ}", memory: "${MEM_REQ}" }
          limits:   { cpu: "${CPU_LIM}", memory: "${MEM_LIM}" }
        volumeMounts:
        - { name: tuning,          mountPath: /tuning }
        - { name: conf,            mountPath: /opt/nifi/nifi-current/conf }
        - { name: flowfile-repo,   mountPath: /opt/nifi/nifi-current/flowfile_repository }
        - { name: content-repo,    mountPath: /opt/nifi/nifi-current/content_repository }
        - { name: provenance-repo, mountPath: /opt/nifi/nifi-current/provenance_repository }
        - { name: state,           mountPath: /opt/nifi/nifi-current/state }
        startupProbe:
          tcpSocket: { port: 8443 }
          failureThreshold: 120
          periodSeconds: 10
      volumes:
      - name: tuning
        configMap: { name: nifi-cluster-tuning, defaultMode: 0755 }
  volumeClaimTemplates:
  - metadata: { name: conf }
    spec: { accessModes: ["ReadWriteOnce"], storageClassName: ${STORAGE_CLASS}, resources: { requests: { storage: ${CONF_SIZE} } } }
  - metadata: { name: flowfile-repo }
    spec: { accessModes: ["ReadWriteOnce"], storageClassName: ${STORAGE_CLASS}, resources: { requests: { storage: ${FLOWFILE_REPO_SIZE} } } }
  - metadata: { name: content-repo }
    spec: { accessModes: ["ReadWriteOnce"], storageClassName: ${STORAGE_CLASS}, resources: { requests: { storage: ${CONTENT_REPO_SIZE} } } }
  - metadata: { name: provenance-repo }
    spec: { accessModes: ["ReadWriteOnce"], storageClassName: ${STORAGE_CLASS}, resources: { requests: { storage: ${PROVENANCE_REPO_SIZE} } } }
  - metadata: { name: state }
    spec: { accessModes: ["ReadWriteOnce"], storageClassName: ${STORAGE_CLASS}, resources: { requests: { storage: ${STATE_SIZE} } } }
YAML
}

cmd_deploy() {
  log "ns=${NS} sc=${STORAGE_CLASS} nodes=${REPLICAS} image=${NIFI_IMAGE}"
  render | kubectl apply -f -
  log "waiting for ZooKeeper"
  kubectl -n "$NS" rollout status statefulset/zookeeper --timeout=10m \
    || warn "ZooKeeper not ready; NiFi will not form a cluster without it"
  log "waiting for NiFi (flow election adds about a minute)"
  kubectl -n "$NS" rollout status statefulset/nifi --timeout=20m \
    || warn "not ready: kubectl -n $NS logs nifi-0 -c nifi"
  kubectl -n "$NS" get pods -o wide
  log "next: $0 status"
}

cmd_status() {
  kubectl -n "$NS" port-forward pod/nifi-0 "${PORT_BASE}:8443" >/dev/null 2>&1 &
  local pf=$!; trap 'kill $pf 2>/dev/null || true' EXIT
  sleep 4
  local tok
  tok=$(curl -sk --noproxy '*' -X POST "https://127.0.0.1:${PORT_BASE}/nifi-api/access/token" \
    -d "username=${NIFI_USER}&password=${NIFI_PASS}" 2>/dev/null) || die "cannot authenticate"
  curl -sk --noproxy '*' -H "Authorization: Bearer ${tok}" \
    "https://127.0.0.1:${PORT_BASE}/nifi-api/controller/cluster" 2>/dev/null \
    | python3 -c '
import json, sys
d = json.load(sys.stdin).get("cluster", {})
nodes = d.get("nodes", [])
print("cluster: %d node(s)" % len(nodes))
for n in nodes:
    roles = ",".join(n.get("roles") or []) or "-"
    print("  %-45s %-12s roles=%s" % (
        n.get("address", "?"), n.get("status", "?"), roles))
if not any("Cluster Coordinator" in (n.get("roles") or []) for n in nodes):
    print("WARNING: no coordinator elected. This is not yet a working cluster.")
' || die "could not read cluster status"
}

# The test the standalone mode structurally cannot run.
cmd_failover() {
  log "identifying the coordinator"
  cmd_status > /tmp/cluster-before.txt 2>&1 || true
  local coord
  coord=$(grep "Cluster Coordinator" /tmp/cluster-before.txt | awk '{print $1}' | head -1)
  [[ -n "$coord" ]] || die "no coordinator found; is the cluster formed?"
  local pod="${coord%%.*}"
  log "coordinator is ${pod}; deleting it"

  local t0; t0=$(date +%s)
  kubectl -n "$NS" delete pod "$pod" --wait=false >/dev/null
  log "waiting for a new coordinator"
  local elected=0 deadline=$(( t0 + 600 ))
  while [[ "$(date +%s)" -lt "$deadline" ]]; do
    if cmd_status 2>/dev/null | grep -q "Cluster Coordinator"; then
      local new
      new=$(cmd_status 2>/dev/null | grep "Cluster Coordinator" | awk '{print $1}' | head -1)
      if [[ -n "$new" && "$new" != "$coord" ]]; then
        elected=$(date +%s)
        log "new coordinator ${new} after $(( elected - t0 ))s"
        break
      fi
    fi
    sleep 5
  done
  [[ $elected -eq 0 ]] && warn "no new coordinator was elected within 600s"
  cmd_status
}

cmd_ui() {
  log "UI: https://127.0.0.1:${PORT_BASE}/nifi  user=${NIFI_USER} pass=${NIFI_PASS}"
  kubectl -n "$NS" port-forward pod/nifi-0 "${PORT_BASE}:8443"
}

cmd_teardown() {
  read -rp "delete namespace ${NS} and ALL its PVCs? [y/N] " a
  [[ "$a" == "y" ]] || { log "aborted"; exit 0; }
  kubectl delete namespace "$NS" --wait=true
}

case "${1:-}" in
  render)   render ;;
  deploy)   cmd_deploy ;;
  status)   cmd_status ;;
  failover) cmd_failover ;;
  ui)       cmd_ui ;;
  teardown) cmd_teardown ;;
  *) sed -n '3,22p' "$0"; exit 1 ;;
esac
