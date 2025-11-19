# FIO Benchmark Helm Chart

Deploy and manage FIO storage benchmarks on OpenShift/Kubernetes clusters using Helm.

## Features

- 🚀 Deploy multiple FIO benchmark pods with a single command
- 💾 Automatic PVC provisioning for each pod
- 📝 Flexible FIO job configuration (inline or file-based)
- 🔧 Highly configurable via values.yaml
- 🧹 Easy cleanup with `helm uninstall`
- 📊 Supports custom resource limits and scheduling

## Prerequisites

- Helm 3.x installed
- kubectl/oc configured with cluster access
- FIO container image (with FIO 3.41)
- Sufficient cluster resources

## Quick Start

### 1. Install with default values

```bash
helm install my-fio-benchmark ./fio-benchmark-chart
```

### 2. Install with custom values

```bash
helm install my-fio-benchmark ./fio-benchmark-chart \
  --set namePrefix=MyPrefix \
  --set replicaCount=5 \
  --set pvc.size=20Gi \
  --set image.repository=your-registry.com/fio \
  --set image.tag=3.41
```

### 3. Install with custom FIO job file

```bash
helm install my-fio-benchmark ./fio-benchmark-chart \
  --set-file fioJob.content=./jobs/my-custom-job.fio \
  --set replicaCount=10
```

### 4. Install in specific namespace

```bash
helm install my-fio-benchmark ./fio-benchmark-chart \
  --namespace benchmark \
  --create-namespace
```

## Configuration

### Key Parameters

| Parameter | Description | Default |
|-----------|-------------|---------|
| `replicaCount` | Number of FIO pods to deploy | `3` |
| `image.repository` | FIO container image repository | `your-registry.example.com/fio` |
| `image.tag` | FIO container image tag | `3.41` |
| `namePrefix` | Set name name for Pods prefix | `nfs-benchmark` |
| `pvc.size` | Size of PVC for each pod | `10Gi` |
| `pvc.storageClassName` | Storage class name | `""` (default) |
| `mountPath` | Mount path for PVC in pod | `/mnt/fio-data` |
| `namespace` | Kubernetes namespace | `default` |
| `resources.requests.cpu` | CPU request per pod | `500m` |
| `resources.requests.memory` | Memory request per pod | `512Mi` |
| `resources.limits.cpu` | CPU limit per pod | `2000m` |
| `resources.limits.memory` | Memory limit per pod | `2Gi` |

### Custom values.yaml

Create your own `my-values.yaml`:

```yaml
replicaCount: 10

image:
  repository: quay.io/your-org/fio
  tag: "3.41"

pvc:
  size: 50Gi
  storageClassName: fast-ssd

resources:
  requests:
    cpu: 1000m
    memory: 1Gi
  limits:
    cpu: 4000m
    memory: 4Gi

fioJob:
  content: |
    [global]
    ioengine=libaio
    direct=1
    size=10G
    runtime=120
    
    [my-test]
    rw=randread
    bs=4k
    iodepth=64
```

Install with custom values:

```bash
helm install my-benchmark ./fio-benchmark-chart -f my-values.yaml
```

## Usage Examples

### Example 1: Quick Random Read Test

```bash
helm install quick-read ./fio-benchmark-chart \
  --set replicaCount=3 \
  --set pvc.size=5Gi \
  --set-file fioJob.content=./jobs/example.fio
```

### Example 2: Large Scale Sequential Write Test

```bash
helm install large-seq-write ./fio-benchmark-chart \
  --set replicaCount=20 \
  --set pvc.size=100Gi \
  --set pvc.storageClassName=nvme-fast \
  --set resources.limits.cpu=4000m
```

### Example 3: Mixed Workload on Specific Nodes

Create `node-specific-values.yaml`:

```yaml
replicaCount: 5

nodeSelector:
  node-type: storage-optimized

tolerations:
  - key: "workload"
    operator: "Equal"
    value: "storage-benchmark"
    effect: "NoSchedule"
```

Install:

```bash
helm install node-specific ./fio-benchmark-chart -f node-specific-values.yaml
```

## Monitoring Results

### View logs from all pods

```bash
kubectl logs -l app=fio-benchmark --namespace=default
```

### View logs from specific pod

```bash
kubectl logs fio-benchmark-0 --namespace=default
```

### Follow logs in real-time

```bash
kubectl logs -f fio-benchmark-0 --namespace=default
```

### Check pod status

```bash
kubectl get pods -l app=fio-benchmark --namespace=default
```

### Export results

```bash
for i in {0..2}; do
  kubectl logs fio-benchmark-$i > results-pod-$i.txt
done
```

## Cleanup

### Uninstall the release

```bash
helm uninstall my-fio-benchmark
```

This will delete:
- All pods
- All PVCs
- ConfigMap

### Verify cleanup

```bash
kubectl get pods,pvc,configmap -l app=fio-benchmark
```

## Advanced Usage

### Upgrade running benchmark

```bash
helm upgrade my-fio-benchmark ./fio-benchmark-chart \
  --set replicaCount=15 \
  --reuse-values
```

### Dry run to see generated manifests

```bash
helm install my-fio-benchmark ./fio-benchmark-chart \
  --dry-run --debug
```

### Template rendering

```bash
helm template my-fio-benchmark ./fio-benchmark-chart \
  --set replicaCount=5 > rendered-manifests.yaml
```

### List all releases

```bash
helm list
```

### Get release values

```bash
helm get values my-fio-benchmark
```

## FIO Job Configuration

### Creating Custom FIO Jobs

Create a file `my-job.fio`:

```ini
[global]
ioengine=libaio
direct=1
size=10G
runtime=300
time_based=1
group_reporting=1
directory=/mnt/fio-data

[random-read-4k]
rw=randread
bs=4k
iodepth=32
numjobs=4

[random-write-4k]
rw=randwrite
bs=4k
iodepth=32
numjobs=4
```

Use it:

```bash
helm install my-test ./fio-benchmark-chart \
  --set-file fioJob.content=./my-job.fio
```

### FIO Parameters Reference

Common FIO parameters:

- `ioengine`: I/O engine (libaio, sync, psync)
- `direct`: 1 for direct I/O, 0 for buffered
- `size`: Size of test file
- `runtime`: Test duration in seconds
- `rw`: Read/write pattern (read, write, randread, randwrite, randrw)
- `bs`: Block size (4k, 8k, 1M, etc.)
- `iodepth`: I/O depth (queue depth)
- `numjobs`: Number of threads/jobs

## Troubleshooting

### Pods stuck in Pending

Check PVC status:
```bash
kubectl get pvc
```

Check storage class:
```bash
kubectl get storageclass
```

Describe pod for more details:
```bash
kubectl describe pod fio-benchmark-0
```

### Image pull errors

Check image exists and is accessible:
```bash
kubectl describe pod fio-benchmark-0 | grep -A 10 Events
```

Add image pull secret if using private registry:
```yaml
imagePullSecrets:
  - name: my-registry-secret
```

### Out of resources

Check node resources:
```bash
kubectl top nodes
kubectl describe nodes
```

Reduce resource requests:
```bash
helm upgrade my-benchmark ./fio-benchmark-chart \
  --set resources.requests.cpu=250m \
  --set resources.requests.memory=256Mi \
  --reuse-values
```

### FIO not installed in image

Verify FIO is in your image:
```bash
kubectl exec fio-benchmark-0 -- fio --version
```

Build proper image with FIO 3.41 installed.

## Building FIO Container Image

Example Dockerfile:

```dockerfile
FROM ubuntu:22.04

RUN apt-get update && \
    apt-get install -y fio && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

CMD ["bash"]
```

Build and push:

```bash
docker build -t your-registry.com/fio:3.41 .
docker push your-registry.com/fio:3.41
```

## Best Practices

1. **Start Small**: Test with 1-2 pods before scaling
2. **Monitor Resources**: Watch CPU, memory, and I/O during tests
3. **Use StorageClass**: Specify appropriate storage class for workload
4. **Set Limits**: Always set resource limits to prevent node exhaustion
5. **Unique Names**: Keep uniqueNames enabled to avoid conflicts
6. **Clean Up**: Always uninstall after testing
7. **Save Results**: Export logs before cleanup
8. **Version Control**: Keep FIO job files in Git

## Contributing

Contributions are welcome! Please submit pull requests or open issues.

## License

MIT License

## Support

For issues and questions, please open an issue in the repository.
