"""Verdicts against measured SLOs, and diagnoses only where evidence exists.

Two rules, both learned from what the previous parser did wrong.

1. A verdict requires a calibrated SLO. The old scoring produced letter
   grades from thresholds picked by hand -- p99_warn_ms 20.0, stability_warn
   15.0 and so on -- that had no relationship to what NiFi or the array
   actually require. An "A+" against an invented target is worse than no
   grade at all, because it looks like it means something. load_slo refuses
   a file that has not been marked calibrated, and verdict() returns
   pass=None rather than guessing.

2. A diagnosis requires evidence. The old nfs_diagnosis emitted "NFS lock
   contention", "server GC", "network retry" and "server OOM pressure" from
   fio latency numbers alone. None of those can be established from fio
   output. What survives here is only what the collected telemetry can
   actually support: client CPU saturation from usr_cpu, cgroup throttling
   from cpu.stat, and network retransmission from the mountstats ntrans
   counter. Everything else is deleted rather than softened, because a
   hypothesis printed next to real measurements gets read as a finding.
"""

import json
import os


class SloError(Exception):
    pass


def load_slo(path):
    """Load an SLO file, or None when there is not one.

    Absent is fine and simply means no verdict. Present but uncalibrated is
    an error: a template someone forgot to fill in must not quietly pass
    every run.
    """
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path) as fh:
            slo = json.load(fh)
    except ValueError as e:
        raise SloError("%s: invalid JSON: %s" % (path, e))

    if not slo.get("calibrated"):
        raise SloError(
            "%s is not calibrated.\n"
            "  Set \"calibrated\": true only once the thresholds come from\n"
            "  measurements of the target environment, and record where in\n"
            "  \"source\". Until then this suite reports figures, not verdicts."
            % path)
    if not slo.get("source"):
        raise SloError(
            "%s is marked calibrated but has no \"source\".\n"
            "  Record what the thresholds were derived from, so a later\n"
            "  reader can tell whether they still apply." % path)
    return slo


def verdict(agg, slo, test_id):
    """PASS/FAIL against the declared SLO, or no verdict at all.

    Returns {"pass": True|False|None, "reason": str, "breaches": [str]}.
    None is a first-class answer: it means nobody has said what good looks
    like for this test, which is not the same as the run being fine.
    """
    if not slo:
        return {"pass": None, "breaches": [],
                "reason": "no SLO file; figures only, no verdict"}

    target = (slo.get("targets") or {}).get(test_id)
    if not target:
        return {"pass": None, "breaches": [],
                "reason": "no SLO entry for '%s'; figures only, no verdict" % test_id}

    breaches = []

    def _min(key, actual, label, fmt=",.0f"):
        want = target.get(key)
        if want is not None and actual is not None and actual < want:
            breaches.append("%s %s is below the required %s"
                            % (label, format(actual, fmt), format(want, fmt)))

    def _max(key, actual, label, fmt=",.1f"):
        want = target.get(key)
        if want is not None and actual is not None and actual > want:
            breaches.append("%s %s exceeds the permitted %s"
                            % (label, format(actual, fmt), format(want, fmt)))

    _min("min_iops", agg.get("total_iops"), "total IOPS")
    _min("min_bw_mibps", agg.get("total_bw_mibps"), "total MiB/s", ",.1f")
    _max("max_p99_ms", agg.get("read_p99_worst_ms"), "worst read P99 ms")
    _max("max_p99_ms", agg.get("write_p99_worst_ms"), "worst write P99 ms")
    _max("max_p999_ms", agg.get("read_p999_worst_ms"), "worst read P99.9 ms")
    _max("max_p999_ms", agg.get("write_p999_worst_ms"), "worst write P99.9 ms")

    return {
        "pass": not breaches,
        "breaches": breaches,
        "reason": ("met every threshold in %s" % slo["source"]) if not breaches
                  else ("breached %d threshold(s) from %s"
                        % (len(breaches), slo["source"])),
    }


# Thresholds for the observations below. These are not SLOs -- they are the
# points at which a signal becomes worth mentioning, and each one is tied to
# a measurement that was actually collected.
CPU_SATURATED_PCT = 85.0
RETRANS_NOTABLE_PCT = 1.0


def diagnose(pods, telemetry):
    """Observations supported by collected telemetry, and nothing else.

    `telemetry` may carry an "nfs" key holding the per-operation deltas from
    nfsstat.py. Without it, the NFS-side observations are simply not made --
    they are not guessed at from latency.
    """
    out = []

    hot = [p.pod for p in pods
           if p.usr_cpu is not None and p.usr_cpu >= CPU_SATURATED_PCT]
    if hot:
        out.append("client CPU at or above %.0f%% user on %d pod(s) (%s): the "
                   "load generator, not the storage, may be the limit"
                   % (CPU_SATURATED_PCT, len(hot), ", ".join(sorted(hot)[:3])))

    throttled = [(p.pod, p.throttled_usec) for p in pods if p.throttled_usec]
    if throttled:
        worst = max(t for _, t in throttled)
        out.append("cgroup CPU throttling on %d pod(s), worst %.1fs: the tail "
                   "latency measured here is the CFS scheduler and cannot be "
                   "attributed to storage"
                   % (len(throttled), worst / 1e6))

    nfs = (telemetry or {}).get("nfs") or {}
    for op, d in sorted(nfs.items()):
        rt = d.get("retrans_pct")
        if rt is not None and rt >= RETRANS_NOTABLE_PCT:
            out.append("NFS %s retransmitted %.2f%% of RPCs (%s timeouts): the "
                       "client resent requests, which is direct evidence of "
                       "loss or server non-response"
                       % (op, rt, format(d.get("timeouts", 0), ",")))

    return out
