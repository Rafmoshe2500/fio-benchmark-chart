#!/usr/bin/env python3
"""
FIO NFS Benchmark Expert Analyzer — parse_results.py
=====================================================
Parses FIO 3.x group_reporting logs collected from Kubernetes pods.
Produces:
  - Console report: Throughput, Latency, Stability, Scoring tables
  - summary_report.csv
  - scores_report.csv
  - Workload classification & expert recommendations per pod

Scoring Dimensions (each 0–100, weighted into composite):
  1. IOPS Performance Score     — achieved vs workload-class target
  2. Throughput Score           — BW vs workload-class target
  3. Latency Quality Score      — avg clat + tail ratio
  4. P99 Tail Score             — P99 vs SLA thresholds
  5. Stability Score            — IOPS σ consistency across time
  6. Read/Write Balance Score   — deviation from declared R/W ratio
"""

import os
import sys
import glob
import re
import csv
import math
import json
from dataclasses import dataclass, field, asdict
from typing import Optional
from datetime import datetime

# ═══════════════════════════════════════════════════════════════════
# WORKLOAD PROFILES
# Each profile defines expected targets for scoring.
# Detect from TEST_ID name or override with --profile flag.
# ═══════════════════════════════════════════════════════════════════

WORKLOAD_PROFILES = {
    "randread_4k": {
        "desc": "Random Read 4K — IOPS-bound (NFS metadata/DB)",
        "target_iops_r": 50_000,  "target_iops_w": 0,
        "target_bw_r": 200,       "target_bw_w": 0,
        "p99_warn_ms": 2.0,       "p99_fail_ms": 10.0,
        "stability_warn": 10.0,   "stability_fail": 25.0,
        "tail_warn": 5.0,         "tail_fail": 20.0,
        "rw_ratio": 1.0,          # read fraction 0-1
    },
    "randwrite_4k": {
        "desc": "Random Write 4K — IOPS-bound (NFS journal/DB write)",
        "target_iops_r": 0,       "target_iops_w": 30_000,
        "target_bw_r": 0,         "target_bw_w": 120,
        "p99_warn_ms": 5.0,       "p99_fail_ms": 20.0,
        "stability_warn": 15.0,   "stability_fail": 30.0,
        "tail_warn": 8.0,         "tail_fail": 30.0,
        "rw_ratio": 0.0,
    },
    "randrw_4k": {
        "desc": "Mixed Random R/W 4K — IOPS-bound (50/50)",
        "target_iops_r": 20_000,  "target_iops_w": 20_000,
        "target_bw_r": 80,        "target_bw_w": 80,
        "p99_warn_ms": 5.0,       "p99_fail_ms": 25.0,
        "stability_warn": 15.0,   "stability_fail": 30.0,
        "tail_warn": 8.0,         "tail_fail": 30.0,
        "rw_ratio": 0.5,
    },
    "seqread_1m": {
        "desc": "Sequential Read 1M — Throughput-bound (NFS bulk read)",
        "target_iops_r": 3_000,   "target_iops_w": 0,
        "target_bw_r": 3_000,     "target_bw_w": 0,
        "p99_warn_ms": 20.0,      "p99_fail_ms": 100.0,
        "stability_warn": 20.0,   "stability_fail": 40.0,
        "tail_warn": 10.0,        "tail_fail": 40.0,
        "rw_ratio": 1.0,
    },
    "seqwrite_1m": {
        "desc": "Sequential Write 1M — Throughput-bound (NFS bulk write)",
        "target_iops_r": 0,       "target_iops_w": 2_000,
        "target_bw_r": 0,         "target_bw_w": 2_000,
        "p99_warn_ms": 30.0,      "p99_fail_ms": 150.0,
        "stability_warn": 20.0,   "stability_fail": 40.0,
        "tail_warn": 10.0,        "tail_fail": 40.0,
        "rw_ratio": 0.0,
    },
    "seqrw_512k": {
        "desc": "Sequential Mixed R/W 512K — Throughput-bound",
        "target_iops_r": 5_000,   "target_iops_w": 5_000,
        "target_bw_r": 2_500,     "target_bw_w": 2_500,
        "p99_warn_ms": 25.0,      "p99_fail_ms": 120.0,
        "stability_warn": 20.0,   "stability_fail": 40.0,
        "tail_warn": 10.0,        "tail_fail": 40.0,
        "rw_ratio": 0.5,
    },
    "mixed_iops": {
        "desc": "Mixed IOPS workload (various block sizes)",
        "target_iops_r": 15_000,  "target_iops_w": 15_000,
        "target_bw_r": 500,       "target_bw_w": 500,
        "p99_warn_ms": 10.0,      "p99_fail_ms": 50.0,
        "stability_warn": 15.0,   "stability_fail": 30.0,
        "tail_warn": 8.0,         "tail_fail": 30.0,
        "rw_ratio": 0.5,
    },
    "default": {
        "desc": "Generic NFS workload",
        "target_iops_r": 10_000,  "target_iops_w": 10_000,
        "target_bw_r": 500,       "target_bw_w": 500,
        "p99_warn_ms": 10.0,      "p99_fail_ms": 100.0,
        "stability_warn": 15.0,   "stability_fail": 30.0,
        "tail_warn": 10.0,        "tail_fail": 50.0,
        "rw_ratio": 0.5,
    },
}

SCORE_WEIGHTS = {
    "iops":       0.25,
    "throughput": 0.20,
    "latency":    0.20,
    "p99":        0.20,
    "stability":  0.10,
    "balance":    0.05,
}

GRADE_THRESHOLDS = [
    (95, "S",  "🏆 Exceptional"),
    (85, "A+", "✅ Excellent"),
    (75, "A",  "✅ Very Good"),
    (65, "B+", "⚠️  Good"),
    (55, "B",  "⚠️  Acceptable"),
    (45, "C",  "❌ Below Par"),
    (0,  "F",  "❌ Critical"),
]

# ═══════════════════════════════════════════════════════════════════
# UNIT HELPERS
# ═══════════════════════════════════════════════════════════════════

def bw_to_mbps(value: float, unit: str) -> float:
    u = unit.strip()
    if "GiB/s" in u or "GB/s" in u: return value * 1024.0
    if "MiB/s" in u or "MB/s" in u: return value
    if "KiB/s" in u or "kB/s" in u or "KB/s" in u: return value / 1024.0
    return value

def parse_iops_str(s: str) -> float:
    s = s.strip()
    if s.endswith(("k", "K")): return float(s[:-1]) * 1_000.0
    if s.endswith(("m", "M")): return float(s[:-1]) * 1_000_000.0
    return float(s)

def to_ms(value: float, unit: str) -> float:
    return value / 1000.0 if unit == "usec" else value

# ═══════════════════════════════════════════════════════════════════
# METRICS DATA CLASS
# ═══════════════════════════════════════════════════════════════════

@dataclass
class FioMetrics:
    pod: str = ""
    # Throughput
    read_iops:        float = 0.0
    write_iops:       float = 0.0
    read_bw_mbps:     float = 0.0
    write_bw_mbps:    float = 0.0
    # IOPS variability
    read_iops_stdev:  float = 0.0
    write_iops_stdev: float = 0.0
    read_iops_min:    float = 0.0
    read_iops_max:    float = 0.0
    write_iops_min:   float = 0.0
    write_iops_max:   float = 0.0
    # Latency (ms)
    read_lat_ms:      float = 0.0
    write_lat_ms:     float = 0.0
    read_clat_ms:     float = 0.0
    write_clat_ms:    float = 0.0
    read_p50_ms:      float = 0.0
    write_p50_ms:     float = 0.0
    read_p95_ms:      float = 0.0
    write_p95_ms:     float = 0.0
    read_p99_ms:      float = 0.0
    write_p99_ms:     float = 0.0
    read_p999_ms:     float = 0.0
    write_p999_ms:    float = 0.0
    read_clat_max_ms: float = 0.0
    write_clat_max_ms:float = 0.0
    # CPU / sys util (if printed by fio)
    cpu_usr:          float = 0.0
    cpu_sys:          float = 0.0
    ctx_switches:     int   = 0
    parse_warnings:   list  = field(default_factory=list)

# ═══════════════════════════════════════════════════════════════════
# CORE PARSER
# ═══════════════════════════════════════════════════════════════════

def parse_fio_log(filepath: str) -> FioMetrics:
    m = FioMetrics(pod=os.path.basename(filepath).replace(".log", ""))

    with open(filepath, "r", errors="replace") as f:
        lines = f.readlines()

    section = None
    in_clat_pct = False
    clat_pct_unit = "usec"

    for line in lines:
        raw = line.rstrip()
        s = raw.strip()

        # ── read/write section headers ───────────────────────────
        if re.match(r"read\s*:", s):
            section = "read"; in_clat_pct = False
            hit = re.search(r"IOPS=([\d\.kKmM]+),\s*BW=([\d\.]+)([\w/]+)", s)
            if hit:
                m.read_iops    = parse_iops_str(hit.group(1))
                m.read_bw_mbps = bw_to_mbps(float(hit.group(2)), hit.group(3))
            continue

        if re.match(r"write\s*:", s):
            section = "write"; in_clat_pct = False
            hit = re.search(r"IOPS=([\d\.kKmM]+),\s*BW=([\d\.]+)([\w/]+)", s)
            if hit:
                m.write_iops    = parse_iops_str(hit.group(1))
                m.write_bw_mbps = bw_to_mbps(float(hit.group(2)), hit.group(3))
            continue

        if section is None:
            # CPU line (global)
            cpu = re.search(r"cpu\s*:\s*usr=([\d\.]+)%.*sys=([\d\.]+)%.*ctx=([\d]+)", s)
            if cpu:
                m.cpu_usr = float(cpu.group(1))
                m.cpu_sys = float(cpu.group(2))
                m.ctx_switches = int(cpu.group(3))
            continue

        pfx = section  # "read" or "write"

        # ── total lat avg ─────────────────────────────────────────
        hit = re.match(r"\s+lat\s+\((usec|msec)\)\s*:\s*min=[\d\.k]+,\s*max=[\d\.k]+,\s*avg=([\d\.]+)", raw)
        if hit:
            val = to_ms(float(hit.group(2)), hit.group(1))
            if pfx == "read"  and not m.read_lat_ms:  m.read_lat_ms  = val
            if pfx == "write" and not m.write_lat_ms: m.write_lat_ms = val

        # ── clat avg + max ────────────────────────────────────────
        hit = re.match(r"\s+clat\s+\((usec|msec)\)\s*:\s*min=[\d\.k]+,\s*max=([\d\.]+),\s*avg=([\d\.]+)", raw)
        if hit:
            clat_avg = to_ms(float(hit.group(3)), hit.group(1))
            clat_max = to_ms(float(hit.group(2)), hit.group(1))
            if pfx == "read":
                if not m.read_clat_ms:  m.read_clat_ms  = clat_avg
                if not m.read_clat_max_ms: m.read_clat_max_ms = clat_max
            else:
                if not m.write_clat_ms: m.write_clat_ms = clat_avg
                if not m.write_clat_max_ms: m.write_clat_max_ms = clat_max

        # ── clat percentiles block ────────────────────────────────
        if "clat percentiles" in s:
            in_clat_pct = True
            clat_pct_unit = "usec" if "usec" in s else "msec"
            continue

        if in_clat_pct:
            if s.startswith("|"):
                def extract_pct(tag, line_s):
                    hit2 = re.search(rf"{re.escape(tag)}=\[\s*(\d+)\]", line_s)
                    return to_ms(float(hit2.group(1)), clat_pct_unit) if hit2 else None

                for tag, attr_r, attr_w in [
                    ("50.00th",  "read_p50_ms",  "write_p50_ms"),
                    ("95.00th",  "read_p95_ms",  "write_p95_ms"),
                    ("99.00th",  "read_p99_ms",  "write_p99_ms"),
                    ("99.90th",  "read_p999_ms", "write_p999_ms"),
                ]:
                    val2 = extract_pct(tag, s)
                    if val2 is not None:
                        if pfx == "read"  and not getattr(m, attr_r): setattr(m, attr_r, val2)
                        if pfx == "write" and not getattr(m, attr_w): setattr(m, attr_w, val2)
            else:
                in_clat_pct = False

        # ── iops stats (stdev / min / max) ────────────────────────
        hit = re.match(
            r"\s+iops\s*:\s*min=\s*([\d\.]+),\s*max=\s*([\d\.]+),\s*avg=[\d\.]+,\s*stdev=\s*([\d\.]+)", raw)
        if hit:
            mn, mx, sd = float(hit.group(1)), float(hit.group(2)), float(hit.group(3))
            if pfx == "read":
                if not m.read_iops_stdev:  m.read_iops_stdev = sd
                if not m.read_iops_min:    m.read_iops_min   = mn
                if not m.read_iops_max:    m.read_iops_max   = mx
            else:
                if not m.write_iops_stdev: m.write_iops_stdev = sd
                if not m.write_iops_min:   m.write_iops_min   = mn
                if not m.write_iops_max:   m.write_iops_max   = mx

    return m

# ═══════════════════════════════════════════════════════════════════
# WORKLOAD PROFILE DETECTION
# ═══════════════════════════════════════════════════════════════════

def detect_profile(test_id: str) -> str:
    t = test_id.lower()
    if "1m"  in t and ("seq" in t or "read" in t and "write" not in t): return "seqread_1m"
    if "1m"  in t and "write" in t:  return "seqwrite_1m"
    if "512k" in t: return "seqrw_512k"
    if "mixed" in t: return "mixed_iops"
    if ("4k" in t or "4kb" in t):
        if "rw" in t or "5050" in t or "7030" in t: return "randrw_4k"
        if "read" in t and "write" not in t: return "randread_4k"
        return "randwrite_4k"
    return "default"

# ═══════════════════════════════════════════════════════════════════
# SCORING ENGINE
# ═══════════════════════════════════════════════════════════════════

def score_iops(m: FioMetrics, p: dict) -> float:
    tr = p["target_iops_r"]; tw = p["target_iops_w"]
    scores = []
    if tr > 0: scores.append(min(100.0, m.read_iops  / tr * 100.0))
    if tw > 0: scores.append(min(100.0, m.write_iops / tw * 100.0))
    return round(sum(scores) / len(scores), 1) if scores else 0.0

def score_throughput(m: FioMetrics, p: dict) -> float:
    tr = p["target_bw_r"]; tw = p["target_bw_w"]
    scores = []
    if tr > 0: scores.append(min(100.0, m.read_bw_mbps  / tr * 100.0))
    if tw > 0: scores.append(min(100.0, m.write_bw_mbps / tw * 100.0))
    return round(sum(scores) / len(scores), 1) if scores else 0.0

def score_latency(m: FioMetrics, p: dict) -> float:
    """Score avg clat vs P99 tail ratio."""
    p99_warn = p["p99_warn_ms"]; p99_fail = p["p99_fail_ms"]
    tail_warn = p["tail_warn"];  tail_fail = p["tail_fail"]

    def _clat_score(clat, p99):
        if clat <= 0: return 100.0
        tail = p99 / clat if clat > 0 else 1.0
        if tail >= tail_fail:   ts = 0.0
        elif tail >= tail_warn: ts = 50.0 * (tail_fail - tail) / (tail_fail - tail_warn)
        else:                   ts = 100.0 - 50.0 * (tail - 1.0) / max(tail_warn - 1.0, 1.0)
        return max(0.0, min(100.0, ts))

    scores = []
    if m.read_clat_ms  > 0: scores.append(_clat_score(m.read_clat_ms,  m.read_p99_ms))
    if m.write_clat_ms > 0: scores.append(_clat_score(m.write_clat_ms, m.write_p99_ms))
    return round(sum(scores) / len(scores), 1) if scores else 0.0

def score_p99(m: FioMetrics, p: dict) -> float:
    warn = p["p99_warn_ms"]; fail = p["p99_fail_ms"]
    def _s(p99):
        if p99 <= 0:    return 100.0
        if p99 >= fail: return 0.0
        if p99 >= warn: return 50.0 * (fail - p99) / (fail - warn)
        return 100.0 - 50.0 * p99 / warn
    scores = []
    if m.read_p99_ms  > 0: scores.append(_s(m.read_p99_ms))
    if m.write_p99_ms > 0: scores.append(_s(m.write_p99_ms))
    return round(sum(scores) / len(scores), 1) if scores else 0.0

def score_stability(m: FioMetrics, p: dict) -> float:
    sw = p["stability_warn"]; sf = p["stability_fail"]
    def _cv(iops, stdev):
        if iops <= 0: return 0.0
        return stdev / iops * 100.0
    def _s(cv):
        if cv >= sf:   return 0.0
        if cv >= sw:   return 50.0 * (sf - cv) / (sf - sw)
        return 100.0 - 50.0 * cv / sw
    scores = []
    if m.read_iops  > 0 and m.read_iops_stdev  > 0: scores.append(_s(_cv(m.read_iops,  m.read_iops_stdev)))
    if m.write_iops > 0 and m.write_iops_stdev > 0: scores.append(_s(_cv(m.write_iops, m.write_iops_stdev)))
    return round(sum(scores) / len(scores), 1) if scores else 100.0

def score_balance(m: FioMetrics, p: dict) -> float:
    """Penalise deviation from declared R/W ratio."""
    target_r = p["rw_ratio"]
    total_iops = m.read_iops + m.write_iops
    if total_iops <= 0: return 100.0
    actual_r = m.read_iops / total_iops
    deviation = abs(actual_r - target_r)
    return round(max(0.0, 100.0 - deviation * 200.0), 1)

def composite_score(scores: dict) -> float:
    return round(sum(scores[k] * SCORE_WEIGHTS[k] for k in SCORE_WEIGHTS), 1)

def grade(score: float) -> tuple:
    for threshold, letter, label in GRADE_THRESHOLDS:
        if score >= threshold:
            return letter, label
    return "F", "❌ Critical"

def build_scores(m: FioMetrics, p: dict) -> dict:
    s = {
        "iops":       score_iops(m, p),
        "throughput": score_throughput(m, p),
        "latency":    score_latency(m, p),
        "p99":        score_p99(m, p),
        "stability":  score_stability(m, p),
        "balance":    score_balance(m, p),
    }
    s["composite"] = composite_score(s)
    s["grade"], s["label"] = grade(s["composite"])
    return s

# ═══════════════════════════════════════════════════════════════════
# EXPERT ANALYSIS FUNCTIONS
# ═══════════════════════════════════════════════════════════════════

def cv(iops, stdev):
    return (stdev / iops * 100.0) if iops > 0 else 0.0

def tail_ratio(p99, clat):
    return (p99 / clat) if clat > 0 else 0.0

def nfs_diagnosis(m: FioMetrics, p: dict, s: dict) -> list:
    """Return expert diagnostic observations for this pod."""
    findings = []

    # Latency tail issues
    for side, p99, clat, lat in [
        ("Read",  m.read_p99_ms,  m.read_clat_ms,  m.read_lat_ms),
        ("Write", m.write_p99_ms, m.write_clat_ms, m.write_lat_ms),
    ]:
        if clat > 0 and p99 > 0:
            tr = tail_ratio(p99, clat)
            if tr > p["tail_fail"]:
                findings.append(f"⛔ {side}: P99/avg={tr:.0f}x — severe NFS jitter (network retry / server queue contention)")
            elif tr > p["tail_warn"]:
                findings.append(f"⚠️  {side}: P99/avg={tr:.0f}x — moderate tail latency (check NFS server TCP retransmits)")
        if p99 >= p["p99_fail_ms"]:
            findings.append(f"⛔ {side}: P99={p99:.1f}ms exceeds SLA ({p['p99_fail_ms']}ms) — possible NFS lock contention / client rto")
        if lat > 0 and clat > 0 and (lat - clat) / lat > 0.15:
            findings.append(f"ℹ️  {side}: submission latency={(lat-clat)/lat*100:.0f}% of total — NFS client queue depth may be undersized (nfs_max_requests?)")

    # IOPS stability
    for side, iops, stdev in [
        ("Read",  m.read_iops,  m.read_iops_stdev),
        ("Write", m.write_iops, m.write_iops_stdev),
    ]:
        c = cv(iops, stdev)
        if c >= p["stability_fail"]:
            findings.append(f"⛔ {side}: CV={c:.0f}% — highly unstable IOPS (GC / compaction / NFS server OOM pressure?)")
        elif c >= p["stability_warn"]:
            findings.append(f"⚠️  {side}: CV={c:.0f}% — IOPS variance elevated (check NFS server network saturation)")

    # IOPS range
    for side, mn, mx, avg_iops in [
        ("Read",  m.read_iops_min,  m.read_iops_max,  m.read_iops),
        ("Write", m.write_iops_min, m.write_iops_max, m.write_iops),
    ]:
        if mx > 0 and mn > 0:
            ratio = mx / mn
            if ratio > 5:
                findings.append(f"⚠️  {side}: IOPS range={mn:.0f}–{mx:.0f} (ratio {ratio:.1f}x) — burst/drain pattern, possible client-side caching")

    # P99.9 tail
    for side, p999 in [("Read", m.read_p999_ms), ("Write", m.write_p999_ms)]:
        if p999 > p["p99_fail_ms"] * 3:
            findings.append(f"⚠️  {side}: P99.9={p999:.1f}ms — extreme outlier events (NFS server GC / network packet loss?)")

    # CPU
    if m.cpu_usr > 80:
        findings.append(f"⚠️  CPU user={m.cpu_usr:.0f}% — client CPU saturated; may throttle IOPS")
    if m.ctx_switches > 500_000:
        findings.append(f"ℹ️  Context switches={m.ctx_switches:,} — high async I/O overhead; verify iodepth tuning")

    # Clean bill
    if not findings:
        findings.append("✅ No anomalies detected — workload within expected NFS parameters")

    return findings

# ═══════════════════════════════════════════════════════════════════
# CLUSTER-LEVEL ANALYSIS
# ═══════════════════════════════════════════════════════════════════

def cluster_analysis(all_metrics: list, all_scores: list) -> list:
    """Cross-pod findings: outlier detection, fairness, hot-pods."""
    findings = []
    n = len(all_metrics)
    if n < 2:
        return findings

    def _stats(vals):
        vals = [v for v in vals if v > 0]
        if not vals: return 0, 0, 0
        mean = sum(vals) / len(vals)
        sd   = math.sqrt(sum((x - mean)**2 for x in vals) / len(vals))
        return mean, sd, sd / mean * 100 if mean > 0 else 0

    riops = [m.read_iops  for m in all_metrics]
    wiops = [m.write_iops for m in all_metrics]
    rp99  = [m.read_p99_ms  for m in all_metrics if m.read_p99_ms  > 0]
    wp99  = [m.write_p99_ms for m in all_metrics if m.write_p99_ms > 0]

    ri_mean, ri_sd, ri_cv = _stats(riops)
    wi_mean, wi_sd, wi_cv = _stats(wiops)

    if ri_cv > 20 or wi_cv > 20:
        findings.append(f"⚠️  Pod-to-pod IOPS disparity: R-CV={ri_cv:.0f}% W-CV={wi_cv:.0f}% — uneven NFS load distribution (check affinity rules / PVC placement)")

    # Outlier pods (> 2σ below mean)
    for m in all_metrics:
        if ri_mean > 0 and m.read_iops  < ri_mean - 2 * ri_sd:
            findings.append(f"⛔ Outlier pod {m.pod}: Read IOPS={m.read_iops:.0f} ({(m.read_iops/ri_mean*100):.0f}% of cluster avg) — possible node-level issue")
        if wi_mean > 0 and m.write_iops < wi_mean - 2 * wi_sd:
            findings.append(f"⛔ Outlier pod {m.pod}: Write IOPS={m.write_iops:.0f} ({(m.write_iops/wi_mean*100):.0f}% of cluster avg)")

    if rp99:
        rp_mean, _, rp_cv = _stats(rp99)
        if rp_cv > 50:
            findings.append(f"⚠️  Read P99 variance across pods={rp_cv:.0f}% — some pods experiencing disproportionate NFS latency (noisy-neighbor?)")
    if wp99:
        wp_mean, _, wp_cv = _stats(wp99)
        if wp_cv > 50:
            findings.append(f"⚠️  Write P99 variance across pods={wp_cv:.0f}%")

    # Score outliers
    composites = [s["composite"] for s in all_scores]
    sc_mean, sc_sd, _ = _stats(composites)
    for m, s in zip(all_metrics, all_scores):
        if s["composite"] < sc_mean - 2 * sc_sd:
            findings.append(f"⛔ Pod {m.pod} composite score={s['composite']:.0f} is >2σ below cluster avg ({sc_mean:.0f}) — investigate this pod specifically")

    if not findings:
        findings.append("✅ Cluster-wide workload distribution is uniform — no inter-pod anomalies detected")

    return findings

# ═══════════════════════════════════════════════════════════════════
# FORMATTING
# ═══════════════════════════════════════════════════════════════════

LINE_WIDE = 140

def banner(title):
    pad = (LINE_WIDE - len(title) - 2) // 2
    print("=" * LINE_WIDE)
    print(" " * pad + f" {title} ")
    print("=" * LINE_WIDE)

def section(title):
    print(f"\n{'─' * LINE_WIDE}")
    print(f"  {title}")
    print(f"{'─' * LINE_WIDE}")

def table(headers, widths, rows_data, fmt_fn, totals_row=None):
    sep = "─" * (sum(widths) + 3 * (len(widths) - 1))
    head = " │ ".join(f"{h:<{w}}" for h, w in zip(headers, widths))
    print(sep)
    print(head)
    print(sep)
    for r in rows_data:
        print(fmt_fn(r, widths))
    if totals_row:
        print(sep)
        print(totals_row)
    print(sep)

# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(description="FIO NFS Expert Benchmark Analyzer")
    parser.add_argument("results_dir", help="Directory containing .log files")
    parser.add_argument("--profile", default=None, help="Force workload profile key")
    parser.add_argument("--json", action="store_true", help="Also emit results.json")
    args = parser.parse_args()

    results_dir = args.results_dir
    log_files   = sorted(glob.glob(os.path.join(results_dir, "*.log")))

    if not log_files:
        print(f"No .log files found in {results_dir}"); sys.exit(1)

    test_id  = os.path.basename(results_dir.rstrip("/"))
    prof_key = args.profile or detect_profile(test_id)
    profile  = WORKLOAD_PROFILES.get(prof_key, WORKLOAD_PROFILES["default"])

    banner(f"FIO NFS BENCHMARK EXPERT REPORT — {test_id}")
    print(f"  Generated : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Log files : {len(log_files)}")
    print(f"  Profile   : [{prof_key}] {profile['desc']}")
    print(f"  Score weights: " + "  ".join(f"{k}={int(v*100)}%" for k, v in SCORE_WEIGHTS.items()))

    all_metrics = [parse_fio_log(f) for f in log_files]
    all_scores  = [build_scores(m, profile) for m in all_metrics]
    rows        = list(zip(all_metrics, all_scores))

    # ── TABLE 1: Throughput ──────────────────────────────────────
    section("TABLE 1 — THROUGHPUT")
    TH = ["Pod", "R-IOPS", "W-IOPS", "R-IOPS σ", "W-IOPS σ", "R-IOPS min", "R-IOPS max", "W-IOPS min", "W-IOPS max", "R-BW MB/s", "W-BW MB/s"]
    TW = [30, 9, 9, 10, 10, 11, 11, 11, 11, 11, 11]
    def fmt_th(row, w):
        m, _ = row
        return (f"{m.pod:<{w[0]}} │ {m.read_iops:<{w[1]}.0f} │ {m.write_iops:<{w[2]}.0f} │ "
                f"{m.read_iops_stdev:<{w[3]}.1f} │ {m.write_iops_stdev:<{w[4]}.1f} │ "
                f"{m.read_iops_min:<{w[5]}.0f} │ {m.read_iops_max:<{w[6]}.0f} │ "
                f"{m.write_iops_min:<{w[7]}.0f} │ {m.write_iops_max:<{w[8]}.0f} │ "
                f"{m.read_bw_mbps:<{w[9]}.2f} │ {m.write_bw_mbps:<{w[10]}.2f}")
    tot_ri = sum(m.read_iops for m, _ in rows)
    tot_wi = sum(m.write_iops for m, _ in rows)
    tot_rb = sum(m.read_bw_mbps for m, _ in rows)
    tot_wb = sum(m.write_bw_mbps for m, _ in rows)
    totals = (f"{'CLUSTER TOTAL':<{TW[0]}} │ {tot_ri:<{TW[1]}.0f} │ {tot_wi:<{TW[2]}.0f} │ "
              f"{'':>{TW[3]}} │ {'':>{TW[4]}} │ {'':>{TW[5]}} │ {'':>{TW[6]}} │ {'':>{TW[7]}} │ {'':>{TW[8]}} │ "
              f"{tot_rb:<{TW[9]}.2f} │ {tot_wb:<{TW[10]}.2f}")
    table(TH, TW, rows, fmt_th, totals)

    # ── TABLE 2: Latency ─────────────────────────────────────────
    section("TABLE 2 — LATENCY (ms)")
    LH = ["Pod", "R-avg", "W-avg", "R-clat avg", "W-clat avg", "R-P50", "W-P50", "R-P95", "W-P95", "R-P99", "W-P99", "R-P99.9", "W-P99.9", "R-max", "W-max"]
    LW = [30, 7, 7, 11, 11, 7, 7, 7, 7, 7, 7, 8, 8, 8, 8]
    def fmt_lat(row, w):
        m, _ = row
        return (f"{m.pod:<{w[0]}} │ {m.read_lat_ms:<{w[1]}.2f} │ {m.write_lat_ms:<{w[2]}.2f} │ "
                f"{m.read_clat_ms:<{w[3]}.2f} │ {m.write_clat_ms:<{w[4]}.2f} │ "
                f"{m.read_p50_ms:<{w[5]}.2f} │ {m.write_p50_ms:<{w[6]}.2f} │ "
                f"{m.read_p95_ms:<{w[7]}.2f} │ {m.write_p95_ms:<{w[8]}.2f} │ "
                f"{m.read_p99_ms:<{w[9]}.2f} │ {m.write_p99_ms:<{w[10]}.2f} │ "
                f"{m.read_p999_ms:<{w[11]}.2f} │ {m.write_p999_ms:<{w[12]}.2f} │ "
                f"{m.read_clat_max_ms:<{w[13]}.2f} │ {m.write_clat_max_ms:<{w[14]}.2f}")
    def avg_of(attr):
        vals = [getattr(m, attr) for m, _ in rows if getattr(m, attr) > 0]
        return sum(vals) / len(vals) if vals else 0.0
    avgs = (f"{'CLUSTER AVG':<{LW[0]}} │ {avg_of('read_lat_ms'):<{LW[1]}.2f} │ {avg_of('write_lat_ms'):<{LW[2]}.2f} │ "
            f"{avg_of('read_clat_ms'):<{LW[3]}.2f} │ {avg_of('write_clat_ms'):<{LW[4]}.2f} │ "
            f"{avg_of('read_p50_ms'):<{LW[5]}.2f} │ {avg_of('write_p50_ms'):<{LW[6]}.2f} │ "
            f"{avg_of('read_p95_ms'):<{LW[7]}.2f} │ {avg_of('write_p95_ms'):<{LW[8]}.2f} │ "
            f"{avg_of('read_p99_ms'):<{LW[9]}.2f} │ {avg_of('write_p99_ms'):<{LW[10]}.2f} │ "
            f"{avg_of('read_p999_ms'):<{LW[11]}.2f} │ {avg_of('write_p999_ms'):<{LW[12]}.2f} │ "
            f"{avg_of('read_clat_max_ms'):<{LW[13]}.2f} │ {avg_of('write_clat_max_ms'):<{LW[14]}.2f}")
    table(LH, LW, rows, fmt_lat, avgs)

    # ── TABLE 3: Stability ───────────────────────────────────────
    section("TABLE 3 — STABILITY & CONSISTENCY")
    SH = ["Pod", "R-CV%", "W-CV%", "R-tail(P99/avg)", "W-tail(P99/avg)", "R-P99.9/P99", "W-P99.9/P99", "R-IOPS range", "W-IOPS range"]
    SW = [30, 7, 7, 16, 16, 13, 13, 14, 14]
    def fmt_stab(row, w):
        m, _ = row
        rcv = cv(m.read_iops, m.read_iops_stdev)
        wcv = cv(m.write_iops, m.write_iops_stdev)
        rtr = tail_ratio(m.read_p99_ms,  m.read_clat_ms)
        wtr = tail_ratio(m.write_p99_ms, m.write_clat_ms)
        r999 = tail_ratio(m.read_p999_ms,  m.read_p99_ms)
        w999 = tail_ratio(m.write_p999_ms, m.write_p99_ms)
        rrange = f"{m.read_iops_min:.0f}–{m.read_iops_max:.0f}"
        wrange = f"{m.write_iops_min:.0f}–{m.write_iops_max:.0f}"
        return (f"{m.pod:<{w[0]}} │ {rcv:<{w[1]}.1f} │ {wcv:<{w[2]}.1f} │ "
                f"{rtr:<{w[3]}.1f} │ {wtr:<{w[4]}.1f} │ "
                f"{r999:<{w[5]}.1f} │ {w999:<{w[6]}.1f} │ "
                f"{rrange:<{w[7]}} │ {wrange:<{w[8]}}")
    table(SH, SW, rows, fmt_stab)

    # ── TABLE 4: Scores ──────────────────────────────────────────
    section("TABLE 4 — EXPERT SCORES (0–100)")
    SCH = ["Pod", "IOPS", "Throughput", "Latency", "P99", "Stability", "Balance", "Composite", "Grade"]
    SCW = [30, 6, 11, 8, 5, 10, 8, 10, 20]
    def fmt_score(row, w):
        m, s = row
        return (f"{m.pod:<{w[0]}} │ {s['iops']:<{w[1]}.1f} │ {s['throughput']:<{w[2]}.1f} │ "
                f"{s['latency']:<{w[3]}.1f} │ {s['p99']:<{w[4]}.1f} │ {s['stability']:<{w[5]}.1f} │ "
                f"{s['balance']:<{w[6]}.1f} │ {s['composite']:<{w[7]}.1f} │ {s['label']}")
    # overall
    avg_comp = sum(s["composite"] for _, s in rows) / len(rows)
    overall_grade, overall_label = grade(avg_comp)
    cluster_total = (f"{'CLUSTER AVG':<{SCW[0]}} │ "
                     f"{sum(s['iops'] for _,s in rows)/len(rows):<{SCW[1]}.1f} │ "
                     f"{sum(s['throughput'] for _,s in rows)/len(rows):<{SCW[2]}.1f} │ "
                     f"{sum(s['latency'] for _,s in rows)/len(rows):<{SCW[3]}.1f} │ "
                     f"{sum(s['p99'] for _,s in rows)/len(rows):<{SCW[4]}.1f} │ "
                     f"{sum(s['stability'] for _,s in rows)/len(rows):<{SCW[5]}.1f} │ "
                     f"{sum(s['balance'] for _,s in rows)/len(rows):<{SCW[6]}.1f} │ "
                     f"{avg_comp:<{SCW[7]}.1f} │ {overall_label}")
    table(SCH, SCW, rows, fmt_score, cluster_total)

    # ── Per-pod expert diagnostics ───────────────────────────────
    section("PER-POD NFS EXPERT DIAGNOSTICS")
    for m, s in rows:
        print(f"\n  🔎 {m.pod}  [{s['grade']}] {s['label']} (composite: {s['composite']:.1f})")
        for finding in nfs_diagnosis(m, profile, s):
            print(f"     {finding}")

    # ── Cluster-level analysis ───────────────────────────────────
    section("CLUSTER-LEVEL ANALYSIS")
    for f in cluster_analysis(all_metrics, all_scores):
        print(f"  {f}")

    # ── CSV Export ───────────────────────────────────────────────
    summary_csv = os.path.join(results_dir, "summary_report.csv")
    scores_csv  = os.path.join(results_dir, "scores_report.csv")

    sum_cols = [
        "Pod","Read IOPS","Write IOPS","Read IOPS Stdev","Write IOPS Stdev",
        "Read IOPS Min","Read IOPS Max","Write IOPS Min","Write IOPS Max",
        "Read BW (MB/s)","Write BW (MB/s)",
        "Read Lat Avg (ms)","Write Lat Avg (ms)",
        "Read Clat Avg (ms)","Write Clat Avg (ms)",
        "Read P50 (ms)","Write P50 (ms)",
        "Read P95 (ms)","Write P95 (ms)",
        "Read P99 (ms)","Write P99 (ms)",
        "Read P99.9 (ms)","Write P99.9 (ms)",
        "Read Clat Max (ms)","Write Clat Max (ms)",
        "CPU usr%","CPU sys%","Ctx Switches",
        "R-CV%","W-CV%","R-TailRatio","W-TailRatio",
    ]
    with open(summary_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(sum_cols)
        for m in all_metrics:
            w.writerow([
                m.pod,
                round(m.read_iops), round(m.write_iops),
                round(m.read_iops_stdev, 2), round(m.write_iops_stdev, 2),
                round(m.read_iops_min), round(m.read_iops_max),
                round(m.write_iops_min), round(m.write_iops_max),
                round(m.read_bw_mbps, 3), round(m.write_bw_mbps, 3),
                round(m.read_lat_ms, 3), round(m.write_lat_ms, 3),
                round(m.read_clat_ms, 3), round(m.write_clat_ms, 3),
                round(m.read_p50_ms, 3), round(m.write_p50_ms, 3),
                round(m.read_p95_ms, 3), round(m.write_p95_ms, 3),
                round(m.read_p99_ms, 3), round(m.write_p99_ms, 3),
                round(m.read_p999_ms, 3), round(m.write_p999_ms, 3),
                round(m.read_clat_max_ms, 3), round(m.write_clat_max_ms, 3),
                round(m.cpu_usr, 1), round(m.cpu_sys, 1), m.ctx_switches,
                round(cv(m.read_iops, m.read_iops_stdev), 1),
                round(cv(m.write_iops, m.write_iops_stdev), 1),
                round(tail_ratio(m.read_p99_ms,  m.read_clat_ms), 2),
                round(tail_ratio(m.write_p99_ms, m.write_clat_ms), 2),
            ])

    sc_cols = ["Pod","IOPS Score","Throughput Score","Latency Score","P99 Score",
               "Stability Score","Balance Score","Composite Score","Grade","Label","Profile"]
    with open(scores_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(sc_cols)
        for m, s in rows:
            w.writerow([
                m.pod, s["iops"], s["throughput"], s["latency"], s["p99"],
                s["stability"], s["balance"], s["composite"], s["grade"], s["label"], prof_key,
            ])

    # Optional JSON
    if args.json:
        json_out = os.path.join(results_dir, "results.json")
        with open(json_out, "w") as f:
            json.dump([{
                "pod": m.pod,
                "metrics": {k: v for k, v in asdict(m).items() if k != "parse_warnings"},
                "scores": s,
                "profile": prof_key,
                "diagnostics": nfs_diagnosis(m, profile, s),
            } for m, s in rows], f, indent=2)
        print(f"\n  JSON saved to: {json_out}")

    print(f"\n  summary_report.csv → {summary_csv}")
    print(f"  scores_report.csv  → {scores_csv}")
    print(f"\n{'═' * LINE_WIDE}")
    print(f"  OVERALL CLUSTER RESULT: {overall_label}  (avg composite score: {avg_comp:.1f}/100)")
    print(f"{'═' * LINE_WIDE}\n")

if __name__ == "__main__":
    main()
