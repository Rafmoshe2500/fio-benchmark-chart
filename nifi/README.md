# NiFi on Kubernetes as an NFS storage load generator

Two files, no Helm, no subscription. Apache NiFi is Apache 2.0 licensed and the
`apache/nifi` image is free.

- `nifi-nfs-loadtest.sh` — deploys the nodes, drives the test
- `nififlow.py` — talks to the NiFi REST API (stdlib only, no pip install)

Keep them in the same directory.

## Quick start

```bash
chmod +x nifi-nfs-loadtest.sh nififlow.py
oc get sc                                    # pick your NFS StorageClass

STORAGE_CLASS=<your-sc> ./nifi-nfs-loadtest.sh all
```

`all` runs the whole sequence and stops at the first real problem:
preflight (StorageClass, SCC, capacity) then smoke (a throwaway pod proving
uid 1000 can actually write) then deploy, flow and stats. Nothing beyond the
1 Gi smoke PVCs is created until the smoke test passes, so a failure costs you
about a minute rather than a full rollout.

Run the stages individually if you'd rather:

```bash
./nifi-nfs-loadtest.sh preflight
./nifi-nfs-loadtest.sh smoke
REPLICAS=3 PROFILE=smallfile ./nifi-nfs-loadtest.sh deploy
./nifi-nfs-loadtest.sh flow
./nifi-nfs-loadtest.sh stats
```

`deploy` is idempotent — re-run it after changing any tunable. `render` prints
the manifests without applying, if you'd rather review or `oc apply` them
yourself.

## OpenShift

The script auto-detects OpenShift (it looks for the `security.openshift.io`
API group) and adjusts accordingly. Two things matter.

**The default SCC will reject these pods.** `restricted-v2` uses
`runAsUser: MustRunAsRange` and assigns a random uid from the namespace's
`openshift.io/sa.scc.uid-range` annotation. The `apache/nifi` image is built
around uid 1000 — `/opt/nifi` is owned by `nifi:nifi` (1000:1000) and is not
group-writable by GID 0 — so an arbitrary uid cannot write to it and NiFi will
not start. Without a fix you get:

```
unable to validate against any security context constraint
```

`deploy` creates a dedicated ServiceAccount (`nifi-loadtest`) and tries to bind
`nonroot-v2` to it. If you aren't a cluster admin, have one run:

```bash
oc adm policy add-scc-to-user nonroot-v2 -z nifi-loadtest -n nifi-loadtest
```

`nonroot-v2` is the smallest privilege that works here — it permits any
non-root uid the pod asks for, and the pod spec already satisfies its other
requirements (`allowPrivilegeEscalation: false`, all capabilities dropped,
`seccompProfile: RuntimeDefault`). Do not reach for `anyuid`; it isn't needed.

If your cluster policy won't allow binding an SCC at all, the alternative is a
derived image that works under an arbitrary uid:

```dockerfile
FROM apache/nifi:2.11.0
USER root
RUN chgrp -R 0 /opt/nifi && chmod -R g=u /opt/nifi
USER 1000
```

Push it, set `NIFI_IMAGE=` to it, and delete the `runAsUser`/`runAsGroup`
lines from the pod `securityContext` in the script. Note that this only fixes
the image's own directories — the NFS-backed PVCs still have to be writable by
whatever uid OpenShift assigns, which usually means a permissive export.

**`fsGroup` and root_squash.** kubelet applies `fsGroup` by recursively
chowning the mounted volume. Against an NFS export with root_squash that chown
fails and the pod never starts. If the smoke test shows the pod stuck in
`Pending` or `CreateContainerError`, re-run with `FSGROUP=` (empty) to omit the
field entirely and let the export's own permissions govern:

```bash
FSGROUP= STORAGE_CLASS=<your-sc> ./nifi-nfs-loadtest.sh smoke
```

**The NFS export must permit uid 1000.** The SCC governs what the cluster
allows; the export governs what the array allows. Both have to agree. If the
export root-squashes or restricts uid 1000, the pod passes admission and then
fails in the init container. Check it with:

```bash
oc -n nifi-loadtest logs nifi-0 -c seed-conf
```

## What gets created

Per node, four RWO PVCs on your StorageClass, each mounted at the path NiFi
already expects, so no path reconfiguration is needed:

| PVC | Mount | What it exercises |
|---|---|---|
| `conf` | `conf/` | flow definition; on NFS so the flow survives a pod restart |
| `flowfile-repo` | `flowfile_repository/` | write-ahead log, small synchronous writes, periodic checkpoint fsync |
| `content-repo` | `content_repository/` | the bulk data path, claim files |
| `provenance-repo` | `provenance_repository/` | Lucene index writes and merges |

Plus one shared RWX PVC (`nifi-data`, mounted at `/data`) that every node writes
to simultaneously — the concurrent-writer test against a single export.

An init container seeds `conf/` from the image on first boot only, because a
freshly provisioned PVC is empty and NiFi needs `nifi.properties` to start.

## Load profiles

```bash
PROFILE=smallfile ./nifi-nfs-loadtest.sh deploy   # default
```

| Profile | Shape | Stresses |
|---|---|---|
| `smallfile` | 4 KB × 200 per batch, 8 threads | flowfile-repo WAL + metadata IOPS. This is the one that hurts NFS. |
| `bigfile` | 16 MB × 1, 4 threads | sequential content-repo throughput |
| `churn` | 512 KB with 3 rewrite hops | content-claim turnover + provenance event volume |

Override any individual knob: `FILE_SIZE="64 KB" BATCH_SIZE=50 CONCURRENT=12`.

## The knobs that actually change the I/O pattern

These are set in `nifi.properties` by the tuning ConfigMap:

```bash
ALWAYS_SYNC=true            # fsync on every repo write. Worst case for NFS.
                            # Run this once to find your floor.
MAX_APPENDABLE_SIZE="1 MB"  # how much NiFi packs into one content claim.
                            # Set to "8 KB" to force ~one file per FlowFile
                            # and generate a metadata storm.
CHECKPOINT_INTERVAL="20 secs"  # lower = more frequent WAL fsync spikes
ARCHIVE_ENABLED=true        # roughly doubles content-repo footprint and writes
BP_OBJECTS=20000            # backpressure threshold; lower it to watch the
                            # pipeline stall and recover
```

`MAX_APPENDABLE_SIZE` is the single most interesting one for a storage
administrator. Small claims turn a throughput test into a metadata test, and
that is usually where an NFS array falls over first.

## Reading the output

`stats` prints, per node, every 10 seconds:

```
nifi-0   in=     1.2 GB  out=     3.4 GB  queued=     120 (4 MB)
         ff-repo   2.00G/20.0%   content  21.00G/42.0%   provenance   5.00G/25.0%
```

- **queued climbing steadily** — storage can't keep up with the generator.
  That's your answer; note the rate at which it stalls.
- **queued flat at the backpressure threshold** — you've found the ceiling.
- **content repo growing but `out` flat** — writes are landing but the read
  side is stalling, usually NFS latency on claim reads.

Pair this with array-side metrics (latency percentiles, op mix, metadata ops/s).
The NiFi numbers tell you what it asked for; the array tells you what it got.

Other views:

```bash
./nifi-nfs-loadtest.sh ui 0      # full NiFi UI, per-processor Status History
./nifi-nfs-loadtest.sh logs 0    # nifi-app.log
kubectl -n nifi-loadtest exec nifi-0 -c nifi -- \
  du -sh /opt/nifi/nifi-current/{flowfile,content,provenance}_repository
```

## Test scenarios worth running

**Find the ceiling.** Start at `CONCURRENT=2`, step up, watch where `queued`
stops draining.

**Metadata storm.** `PROFILE=smallfile MAX_APPENDABLE_SIZE="8 KB"` — the number
of files NiFi creates goes up by orders of magnitude at the same byte volume.

**Restart recovery.** With load running, `kubectl delete pod nifi-1`. Time how
long NiFi takes to replay the flowfile repository and resume. On a slow NFS
mount this is often minutes, and it's the number that matters for an outage.

**Concurrent writers.** Raise `REPLICAS` with `WRITE_OUTPUT=true` — all nodes
hammer the same RWX export at once.

**fsync floor.** `ALWAYS_SYNC=true` on an otherwise identical run. The delta
between the two runs is what your array's sync path costs you.

## Things that will bite you

**uid 1000 and the SCC.** See the OpenShift section above — this is the failure
you are most likely to hit, and it has two independent causes (cluster admission
and export permissions) that look similar from the outside.

**Image tag.** The default is `apache/nifi:2.11.0`. Verify that tag exists and
that your cluster can pull it; if you're behind a registry mirror, mirror it
first and set `NIFI_IMAGE=` to the internal reference.

**Password length.** NiFi 2.x refuses to start if the single-user password is
under 12 characters. The default here is compliant; if you override `NIFI_PASS`,
keep it long.

**NFSv3 locking.** NFSv3 does locking out of band via NLM. The provenance
repository is a Lucene index, and Lucene's file locking over NFSv3 is
long-known to be unreliable. Lock errors in `nifi-app.log` against an NFSv3
StorageClass are a finding about the protocol, not a bug in the setup. If your
cluster also offers NFSv4.1, running the identical test against both and
comparing is usually the most informative thing you can do:

```bash
NS=nifi-nfs3  STORAGE_CLASS=sc-nas-nfs3  ./nifi-nfs-loadtest.sh all
NS=nifi-nfs41 STORAGE_CLASS=sc-nas-nfs41 ./nifi-nfs-loadtest.sh all
```

**NiFi does not officially support NFS for repositories.** File locking and
fsync semantics over NFS are exactly why. That's fine — testing whether your
array can nonetheless carry it is the point of this exercise — but if a run
produces repository corruption or a node that won't recover, that is a
legitimate finding, not a broken script.

**These are independent nodes, not a NiFi cluster.** Clustering requires mutual
TLS between nodes (keystore/truststore generation, SAN entries per pod), which
adds a lot of setup and changes nothing about the per-node storage I/O pattern —
each cluster node has its own repositories either way. If you specifically need
cluster semantics (load-balanced connections, a cluster coordinator), that's a
different build.

**Property names.** The flow builder no longer hardcodes them. It creates each
processor bare, reads back the property descriptors that your NiFi build
actually exposes, and matches the requested names against both the canonical
name and the display name, ignoring case and punctuation. Values constrained to
an allowable set are checked the same way. Anything it cannot resolve is printed
as a warning naming the available properties, rather than silently producing an
invalid processor.

## Teardown

```bash
./nifi-nfs-loadtest.sh teardown   # prompts, then deletes the namespace and PVCs
```
