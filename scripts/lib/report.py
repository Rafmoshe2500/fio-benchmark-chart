"""Turn validated pod results into something reportable.

Two deliberate omissions: there are no letter grades, and there are no
diagnoses.

A grade implies a calibrated SLO and this suite has none yet -- the old
thresholds were picked by hand and had no relationship to what NiFi or the
array actually require. A claim like "NFS lock contention" or "server GC"
cannot be made from fio output alone; it needs array-side telemetry. Both
come back once there is something real to calibrate against.
"""

import csv
import json

BYTES_PER_MIB = 1024.0 ** 2


def cluster_percentile(pods, direction, attr):
    """The cluster's tail is the worst pod's tail, not the average of tails.

    Averaging P99 across ten pods turns one pod at 400ms into a reported
    44ms and hides the only pod anyone cares about. For a true merged
    distribution see merged_percentile().
    """
    vals = []
    for p in pods:
        d = getattr(p, direction, None)
        v = getattr(d, attr, None) if d else None
        if v is not None:
            vals.append(v)
    return max(vals) if vals else None


def merged_percentile(pod_bins, q):
    """True cluster percentile from merged json+ histogram bins.

    json+ emits clat_ns.bins as {nanoseconds: count}. Summing the counts
    across pods and walking to the qth sample gives the real distribution,
    which max-of-p99 only approximates.
    """
    merged = {}
    for bins in pod_bins:
        for ns, count in (bins or {}).items():
            try:
                key = float(ns)
            except (TypeError, ValueError):
                continue
            merged[key] = merged.get(key, 0) + int(count)
    total = sum(merged.values())
    if total == 0:
        return None
    target = q * total
    seen = 0
    for ns in sorted(merged):
        seen += merged[ns]
        if seen >= target:
            return ns / 1_000_000.0
    return None


def _sum(pods, direction, attr):
    vals = [getattr(getattr(p, direction, None), attr, None) for p in pods]
    vals = [v for v in vals if v is not None]
    return sum(vals) if vals else None


def aggregate(pods, meta):
    """Cluster totals plus attainment against the declared target.

    Attainment is None when no target was declared. A ceiling test has no
    target by design, and inventing one is how the old parser scored a
    2,000 IOPS per pod job against a 15,000 IOPS profile.
    """
    r_iops = _sum(pods, "read", "iops") or 0.0
    w_iops = _sum(pods, "write", "iops") or 0.0
    r_bw = _sum(pods, "read", "bw_mibps") or 0.0
    w_bw = _sum(pods, "write", "bw_mibps") or 0.0

    target_iops = meta.get("target_iops_total")
    target_bw = meta.get("target_bw_mibps_total")

    return {
        "pods": len(pods),
        "read_iops_total": r_iops,
        "write_iops_total": w_iops,
        "total_iops": r_iops + w_iops,
        "read_bw_mibps_total": r_bw,
        "write_bw_mibps_total": w_bw,
        "total_bw_mibps": r_bw + w_bw,
        "read_p99_worst_ms": cluster_percentile(pods, "read", "clat_p99_ms"),
        "write_p99_worst_ms": cluster_percentile(pods, "write", "clat_p99_ms"),
        "read_p999_worst_ms": cluster_percentile(pods, "read", "clat_p999_ms"),
        "write_p999_worst_ms": cluster_percentile(pods, "write", "clat_p999_ms"),
        "read_p99_merged_ms": merged_percentile(
            [getattr(p.read, "clat_bins", None) for p in pods if p.read], 0.99),
        "write_p99_merged_ms": merged_percentile(
            [getattr(p.write, "clat_bins", None) for p in pods if p.write], 0.99),
        "iops_attainment_pct": (
            100.0 * (r_iops + w_iops) / target_iops if target_iops else None),
        "bw_attainment_pct": (
            100.0 * (r_bw + w_bw) / target_bw if target_bw else None),
    }


def _f(v, spec):
    return "n/a".rjust(len(format(0, spec))) if v is None else format(v, spec)


def print_report(pods, meta, agg, validation):
    w = 96
    print("=" * w)
    kind = "rate-limited" if meta.get("rate_limited") else "ceiling (uncapped)"
    print("  %s   %d pods   %s" % (meta["test_id"], len(pods), kind))
    print("=" * w)

    if not validation.ok:
        print()
        print("  RUN REJECTED")
        print("  No metrics are reported for a run that did not complete as declared.")
        print()
        for f in validation.failures:
            print("    FAIL  %s" % f)
        print()
        print("=" * w)
        return

    for msg in validation.warnings:
        print("  warn  %s" % msg)

    print()
    print("%-28s %10s %10s %10s %10s %9s %9s"
          % ("pod", "R-IOPS", "W-IOPS", "R-MiB/s", "W-MiB/s", "R-P99ms", "W-P99ms"))
    print("-" * w)
    for p in sorted(pods, key=lambda x: x.pod):
        print("%-28s %s %s %s %s %s %s" % (
            p.pod,
            _f(p.read.iops, "10.0f"), _f(p.write.iops, "10.0f"),
            _f(p.read.bw_mibps, "10.1f"), _f(p.write.bw_mibps, "10.1f"),
            _f(p.read.clat_p99_ms, "9.2f"), _f(p.write.clat_p99_ms, "9.2f")))
    print("-" * w)
    print("%-28s %10.0f %10.0f %10.1f %10.1f %s %s" % (
        "CLUSTER", agg["read_iops_total"], agg["write_iops_total"],
        agg["read_bw_mibps_total"], agg["write_bw_mibps_total"],
        _f(agg["read_p99_worst_ms"], "9.2f"), _f(agg["write_p99_worst_ms"], "9.2f")))
    print()
    print("  P99 on the CLUSTER row is the worst pod, not an average of pods.")
    if agg["read_p99_merged_ms"] is not None:
        print("  Merged-histogram P99 (json+ bins): read %s ms, write %s ms" % (
            _f(agg["read_p99_merged_ms"], ".2f"),
            _f(agg["write_p99_merged_ms"], ".2f")))

    print()
    if agg["iops_attainment_pct"] is not None:
        print("  IOPS attainment: %.1f%% of the declared %s total"
              % (agg["iops_attainment_pct"], format(meta["target_iops_total"], ",")))
    if agg["bw_attainment_pct"] is not None:
        print("  Bandwidth attainment: %.1f%% of the declared %s MiB/s total"
              % (agg["bw_attainment_pct"], format(meta["target_bw_mibps_total"], ",")))
    if agg["iops_attainment_pct"] is None and agg["bw_attainment_pct"] is None:
        print("  No target declared for this test; these are ceiling figures,")
        print("  not a verdict against a requirement.")
    print("=" * w)


def write_csv(path, pods):
    cols = ["pod", "read_iops", "write_iops", "read_mibps", "write_mibps",
            "read_clat_mean_ms", "write_clat_mean_ms",
            "read_p50_ms", "read_p95_ms", "read_p99_ms", "read_p999_ms",
            "write_p50_ms", "write_p95_ms", "write_p99_ms", "write_p999_ms",
            "usr_cpu", "sys_cpu", "ctx_switches", "throttled_usec"]
    with open(path, "w", newline="") as fh:
        wr = csv.writer(fh)
        wr.writerow(cols)
        for p in sorted(pods, key=lambda x: x.pod):
            wr.writerow([
                p.pod, p.read.iops, p.write.iops, p.read.bw_mibps, p.write.bw_mibps,
                p.read.clat_mean_ms, p.write.clat_mean_ms,
                p.read.clat_p50_ms, p.read.clat_p95_ms,
                p.read.clat_p99_ms, p.read.clat_p999_ms,
                p.write.clat_p50_ms, p.write.clat_p95_ms,
                p.write.clat_p99_ms, p.write.clat_p999_ms,
                p.usr_cpu, p.sys_cpu, p.ctx_switches, p.throttled_usec,
            ])


def write_json(path, meta, agg, validation, pods):
    with open(path, "w") as fh:
        json.dump({
            "test_id": meta["test_id"],
            "valid": validation.ok,
            "failures": validation.failures,
            "warnings": validation.warnings,
            "declared": {
                "replicas": meta.get("replicas"),
                "target_iops_total": meta.get("target_iops_total"),
                "target_bw_mibps_total": meta.get("target_bw_mibps_total"),
                "expected_directions": meta.get("expected_directions"),
                "rate_limited": meta.get("rate_limited"),
            },
            "aggregate": agg,
            "pods": [{
                "pod": p.pod, "error": p.error, "elapsed_s": p.elapsed_s,
                "read_iops": p.read.iops, "write_iops": p.write.iops,
                "read_p99_ms": p.read.clat_p99_ms,
                "write_p99_ms": p.write.clat_p99_ms,
                "throttled_usec": p.throttled_usec,
            } for p in sorted(pods, key=lambda x: x.pod)],
        }, fh, indent=2)
