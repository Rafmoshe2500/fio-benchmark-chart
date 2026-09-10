"""Parse fio's json+ output into a strict, validated result.

The rule this module enforces: a metric fio did not report is None, not zero.

The previous text parser initialised every field to 0.0, so a run in which
the entire write half died with ENOSPC produced real read numbers, zeros for
everything on the write side, and every zero fell below every warning
threshold. Ten failed pods were reported as four PASS and six WARN with no
mention of the error. See results/test4_10pods_50k_5050_32kb.
"""

import json


class MissingMetric(Exception):
    """Raised when the JSON is absent, truncated or missing a required key."""


NS_PER_MS = 1_000_000.0
BYTES_PER_MIB = 1024.0 ** 2

# Above this share of wall time spent fully stalled by the CFS scheduler, a
# run is rejected rather than reported.
#
# Not zero, which is what this was first written as. A CFS period is 100ms,
# so throttled time divided by 100ms is the number of fully stalled periods,
# and each can delay at most the outstanding queue depth. On a 600s ceiling
# run at 1024 queue depth, 1.2% of wall time works out at roughly 0.08% of
# IOs -- inside the p99.9 tail and nowhere near p99 or the throughput. The
# zero-tolerance rule discarded runs like that entirely, which reported
# nothing at all about an array that was performing fine.
#
# 5% is where roughly 1 IO in 300 is affected and p99 itself starts to move.
# Tighten it with --max-throttle-pct when the run exists to make a latency
# claim rather than to find a ceiling.
MAX_THROTTLE_PCT = 5.0

CGROUP_BEGIN = "===FIO_CGROUP_BEGIN==="
CGROUP_END = "===FIO_CGROUP_END==="


def _pct(percentile_map, key):
    """fio writes percentile keys as '99.000000'. Missing means missing."""
    if not percentile_map:
        return None
    for k, v in percentile_map.items():
        try:
            if abs(float(k) - key) < 1e-6:
                return float(v) / NS_PER_MS
        except (TypeError, ValueError):
            continue
    return None


class DirectionResult:
    """Plain class rather than a dataclass: dataclasses need Python 3.7 and
    RHEL 8 ships 3.6 as its default python3."""

    def __init__(self, name, iops=None, bw_mibps=None, io_bytes=None,
                 runtime_ms=None, iops_stddev=None, clat_mean_ms=None,
                 clat_stddev_ms=None, clat_p50_ms=None, clat_p95_ms=None,
                 clat_p99_ms=None, clat_p999_ms=None, clat_bins=None):
        self.name = name
        self.iops = iops
        self.bw_mibps = bw_mibps
        self.io_bytes = io_bytes
        self.runtime_ms = runtime_ms
        self.iops_stddev = iops_stddev
        self.clat_mean_ms = clat_mean_ms
        self.clat_stddev_ms = clat_stddev_ms
        self.clat_p50_ms = clat_p50_ms
        self.clat_p95_ms = clat_p95_ms
        self.clat_p99_ms = clat_p99_ms
        self.clat_p999_ms = clat_p999_ms
        self.clat_bins = clat_bins

    @property
    def had_io(self):
        return bool(self.io_bytes) and bool(self.runtime_ms)


class PodResult:
    def __init__(self, pod, error=0, elapsed_s=None, read=None, write=None,
                 usr_cpu=None, sys_cpu=None, ctx_switches=None,
                 throttled_usec=None):
        self.pod = pod
        self.error = error
        self.elapsed_s = elapsed_s
        self.read = read
        self.write = write
        self.usr_cpu = usr_cpu
        self.sys_cpu = sys_cpu
        self.ctx_switches = ctx_switches
        self.throttled_usec = throttled_usec

    @property
    def directions_with_io(self):
        return {d.name for d in (self.read, self.write) if d and d.had_io}


def _direction(name, blob):
    """Build a DirectionResult.

    A direction fio reported with zero bytes is recorded as present-but-idle:
    io_bytes 0, every derived figure None. That lets the caller distinguish
    'did not run' from 'ran and was fast', which zeros cannot.
    """
    if blob is None:
        return DirectionResult(name=name)

    io_bytes = blob.get("io_bytes")
    runtime = blob.get("runtime")
    had_io = bool(io_bytes) and bool(runtime)

    d = DirectionResult(
        name=name,
        io_bytes=io_bytes,
        runtime_ms=runtime,
        iops=blob.get("iops") if had_io else None,
        bw_mibps=(blob.get("bw_bytes", 0) / BYTES_PER_MIB) if had_io else None,
        iops_stddev=blob.get("iops_stddev") if had_io else None,
    )
    if not had_io:
        return d

    clat = blob.get("clat_ns") or {}
    mean = clat.get("mean")
    d.clat_mean_ms = mean / NS_PER_MS if mean else None
    sd = clat.get("stddev")
    d.clat_stddev_ms = sd / NS_PER_MS if sd else None
    d.clat_bins = clat.get("bins") or None

    p = clat.get("percentile") or {}
    d.clat_p50_ms = _pct(p, 50.0)
    d.clat_p95_ms = _pct(p, 95.0)
    d.clat_p99_ms = _pct(p, 99.0)
    d.clat_p999_ms = _pct(p, 99.9)
    return d


def parse_pod_json(pod, text):
    try:
        doc = json.loads(text)
    except (ValueError, TypeError) as e:
        raise MissingMetric("%s: fio JSON is absent or truncated: %s" % (pod, e))

    jobs = doc.get("jobs")
    if not jobs:
        raise MissingMetric("%s: fio JSON has no 'jobs' array" % pod)

    # group_reporting=1 collapses every clone into one entry.
    j = jobs[0]
    return PodResult(
        pod=pod,
        error=int(j.get("error", 0)),
        elapsed_s=j.get("elapsed"),
        read=_direction("read", j.get("read")),
        write=_direction("write", j.get("write")),
        usr_cpu=j.get("usr_cpu"),
        sys_cpu=j.get("sys_cpu"),
        ctx_switches=j.get("ctx"),
    )


def parse_cgroup_throttling(log_text):
    """Microseconds of CPU throttling during the run, or None if not recorded.

    Non-zero means some of the latency fio measured was the CFS scheduler
    stalling the container, not the storage responding slowly. There is no
    way to separate the two after the fact, so the run is rejected.

    None and 0 are different answers: None is 'not measured', 0 is 'measured
    and none happened'.
    """
    if CGROUP_BEGIN not in log_text or CGROUP_END not in log_text:
        return None
    block = log_text.split(CGROUP_BEGIN, 1)[1].split(CGROUP_END, 1)[0]

    def value_of(tag):
        for line in block.splitlines():
            if not line.startswith(tag + ":"):
                continue
            tokens = line.split(":", 1)[1].split()
            for key in ("throttled_usec", "throttled_time"):
                if key in tokens:
                    idx = tokens.index(key)
                    if idx + 1 < len(tokens):
                        try:
                            return int(tokens[idx + 1])
                        except ValueError:
                            return None
        return None

    before, after = value_of("before"), value_of("after")
    if before is None or after is None:
        return None
    return after - before


class RunValidation:
    def __init__(self):
        self.ok = True
        self.failures = []
        self.warnings = []

    def fail(self, msg):
        self.ok = False
        self.failures.append(msg)

    def warn(self, msg):
        self.warnings.append(msg)


def validate_run(pods, meta, max_throttle_pct=MAX_THROTTLE_PCT):
    """Decide whether this run may be reported at all.

    A run that fails here produces no tables and no summary files. Reporting
    numbers from a partially dead run is the failure mode this whole exercise
    exists to remove.
    """
    v = RunValidation()
    expected = set(meta.get("expected_directions") or [])

    want_pods = int(meta.get("replicas", 0) or 0)
    if want_pods and len(pods) != want_pods:
        v.fail("expected %d pods, got %d" % (want_pods, len(pods)))

    for p in pods:
        if p.error:
            hint = ("ENOSPC - the PVC could not hold size x numjobs"
                    if p.error == 28 else "see the pod log")
            v.fail("%s: fio error %d (%s)" % (p.pod, p.error, hint))

        for d in sorted(expected - p.directions_with_io):
            v.fail("%s: no I/O in expected direction '%s'" % (p.pod, d))

        for d in sorted(p.directions_with_io - expected):
            v.warn("%s: unexpected I/O in direction '%s'" % (p.pod, d))

        for d in (p.read, p.write):
            if d and d.had_io and d.clat_p99_ms is None:
                v.fail("%s: %s has I/O but no clat percentiles; "
                       "was the log truncated?" % (p.pod, d.name))

        want_runtime = meta.get("runtime_s")
        if want_runtime and p.elapsed_s:
            # ramp_time is excluded from runtime but included in elapsed, so
            # elapsed should comfortably exceed runtime on a healthy run.
            floor = want_runtime * 0.9
            if p.elapsed_s < floor:
                v.fail("%s: ran %ds, expected at least %.0fs"
                       % (p.pod, p.elapsed_s, floor))

        # None means the cgroup could not be read; 0 means it was read and
        # nothing happened. Only a measured, non-zero value is a finding.
        if p.throttled_usec and p.elapsed_s:
            secs = p.throttled_usec / 1e6
            pct = 100.0 * secs / p.elapsed_s
            if pct >= max_throttle_pct:
                v.fail("%s: CPU cgroup throttled %.1fs of %ds (%.1f%% of wall "
                       "time); at this level the client, not the storage, sets "
                       "the latency. Raise the resource class and re-run."
                       % (p.pod, secs, p.elapsed_s, pct))
            else:
                v.warn("%s: CPU cgroup throttled %.1fs of %ds (%.1f%%); "
                       "throughput and p99 are usable, the p99.9 tail is not "
                       "-- some of it is the CFS scheduler, not the array"
                       % (p.pod, secs, p.elapsed_s, pct))

    return v
