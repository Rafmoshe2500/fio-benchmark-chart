#!/usr/bin/env python3
"""
FIO Log Parser - parse_results.py
Parses FIO 3.x output logs. Extracts per-pod metrics and prints two summary
tables: (1) Throughput, (2) Latency / Stability. Also writes summary_report.csv.

Columns captured:
  Throughput: Read/Write IOPS, Read/Write BW (MB/s), Read/Write IOPS stddev
  Latency:    Total lat avg, clat (completion) avg, P99 clat  — all in ms
"""

import os
import sys
import glob
import re
import csv


# ---------------------------------------------------------------------------
# Unit helpers
# ---------------------------------------------------------------------------

def bw_to_mbps(value: float, unit: str) -> float:
    """Convert raw FIO bandwidth value to MB/s."""
    u = unit.strip()
    if 'GiB/s' in u or 'GB/s' in u:
        return value * 1024.0
    if 'MiB/s' in u or 'MB/s' in u:
        return value
    if 'KiB/s' in u or 'kB/s' in u or 'KB/s' in u:
        return value / 1024.0
    return value


def parse_iops_str(s: str) -> float:
    """Parse IOPS string: '1.5k' -> 1500, '2M' -> 2_000_000."""
    s = s.strip()
    if s.endswith(('k', 'K')):
        return float(s[:-1]) * 1_000.0
    if s.endswith(('m', 'M')):
        return float(s[:-1]) * 1_000_000.0
    return float(s)


def to_ms(value: float, unit: str) -> float:
    """Convert latency value to milliseconds."""
    return value / 1000.0 if unit == 'usec' else value


# ---------------------------------------------------------------------------
# Core parser
# ---------------------------------------------------------------------------

def parse_fio_log(filepath: str) -> dict:
    """
    Parse a single FIO log file (group_reporting output).

    Returns a dict with keys:
        read_iops, read_bw_mbps, read_iops_stdev,
        read_lat_ms, read_clat_ms, read_p99_ms,
        write_iops, write_bw_mbps, write_iops_stdev,
        write_lat_ms, write_clat_ms, write_p99_ms
    """
    metrics = {k: 0.0 for k in [
        'read_iops',  'read_bw_mbps',  'read_iops_stdev',
        'read_lat_ms',  'read_clat_ms',  'read_p99_ms',
        'write_iops', 'write_bw_mbps', 'write_iops_stdev',
        'write_lat_ms', 'write_clat_ms', 'write_p99_ms',
    ]}

    with open(filepath, 'r', errors='replace') as f:
        lines = f.readlines()

    section = None          # 'read' | 'write'
    in_clat_pct = False
    clat_pct_unit = 'usec'

    for line in lines:
        raw = line.rstrip()
        s   = raw.strip()

        # ── Detect top-level read/write blocks ────────────────────────────
        if re.match(r'read\s*:', s):
            section = 'read'
            in_clat_pct = False
            m = re.search(r'IOPS=([\d\.kKmM]+),\s*BW=([\d\.]+)([\w/]+)', s)
            if m:
                metrics['read_iops']    = parse_iops_str(m.group(1))
                metrics['read_bw_mbps'] = bw_to_mbps(float(m.group(2)), m.group(3))
            continue

        if re.match(r'write\s*:', s):
            section = 'write'
            in_clat_pct = False
            m = re.search(r'IOPS=([\d\.kKmM]+),\s*BW=([\d\.]+)([\w/]+)', s)
            if m:
                metrics['write_iops']    = parse_iops_str(m.group(1))
                metrics['write_bw_mbps'] = bw_to_mbps(float(m.group(2)), m.group(3))
            continue

        if section is None:
            continue

        pfx = section + '_'

        # ── Total latency ─────────────────────────────────────────────────
        # "   lat (usec): min=..., avg=25102.24, ..."
        m = re.match(r'\s+lat\s+\((usec|msec)\)\s*:\s*min=[\d\.k]+,\s*max=[\d\.k]+,\s*avg=([\d\.]+)', raw)
        if m and not metrics[pfx + 'lat_ms']:
            metrics[pfx + 'lat_ms'] = to_ms(float(m.group(2)), m.group(1))

        # ── Completion latency avg ────────────────────────────────────────
        # "   clat (usec): min=..., avg=24802.73, ..."
        m = re.match(r'\s+clat\s+\((usec|msec)\)\s*:\s*min=[\d\.k]+,\s*max=[\d\.k]+,\s*avg=([\d\.]+)', raw)
        if m and not metrics[pfx + 'clat_ms']:
            metrics[pfx + 'clat_ms'] = to_ms(float(m.group(2)), m.group(1))

        # ── Start of clat percentiles block ──────────────────────────────
        if 'clat percentiles' in s:
            in_clat_pct   = True
            clat_pct_unit = 'usec' if 'usec' in s else 'msec'
            continue

        # ── Parse P99 from percentile lines ──────────────────────────────
        if in_clat_pct:
            if s.startswith('|'):
                m = re.search(r'99\.00th=\[\s*(\d+)\]', s)
                if m and not metrics[pfx + 'p99_ms']:
                    metrics[pfx + 'p99_ms'] = to_ms(float(m.group(1)), clat_pct_unit)
            else:
                in_clat_pct = False   # end of percentiles block

        # ── IOPS per-job stats (stddev) ───────────────────────────────────
        # "   iops        : min=  2, max= 608, avg=100.99, stdev=16.58, samples=..."
        m = re.match(r'\s+iops\s*:\s*min=\s*[\d\.]+,\s*max=\s*[\d\.]+,\s*avg=[\d\.]+,\s*stdev=([\d\.]+)', raw)
        if m and not metrics[pfx + 'iops_stdev']:
            metrics[pfx + 'iops_stdev'] = float(m.group(1))

    return metrics


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def make_table(rows, headers, col_widths, fmt_row):
    """Return rows of a formatted table as a list of strings."""
    sep = '-' * (sum(col_widths) + 3 * (len(col_widths) - 1))
    head = ' | '.join(f'{h:<{w}}' for h, w in zip(headers, col_widths))
    lines = [sep, head, sep]
    for r in rows:
        lines.append(fmt_row(r, col_widths))
    lines.append(sep)
    return lines


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 parse_results.py <results_directory>")
        sys.exit(1)

    results_dir = sys.argv[1]
    log_files = sorted(glob.glob(os.path.join(results_dir, "*.log")))

    if not log_files:
        print(f"No .log files found in {results_dir}")
        sys.exit(1)

    print(f"\nParsing {len(log_files)} log file(s) from: {results_dir}\n")

    rows        = []
    totals      = {k: 0.0 for k in [
        'read_iops','read_bw_mbps','write_iops','write_bw_mbps']}
    lat_sums    = {k: 0.0 for k in [
        'read_lat_ms','read_clat_ms','read_p99_ms',
        'write_lat_ms','write_clat_ms','write_p99_ms',
        'read_iops_stdev','write_iops_stdev']}
    cnt_read = cnt_write = 0

    for f in log_files:
        pod  = os.path.basename(f).replace('.log', '')
        m    = parse_fio_log(f)
        rows.append((pod, m))

        totals['read_iops']    += m['read_iops']
        totals['read_bw_mbps'] += m['read_bw_mbps']
        totals['write_iops']   += m['write_iops']
        totals['write_bw_mbps']+= m['write_bw_mbps']

        if m['read_iops'] > 0:
            for k in ['read_lat_ms','read_clat_ms','read_p99_ms','read_iops_stdev']:
                lat_sums[k] += m[k]
            cnt_read += 1
        if m['write_iops'] > 0:
            for k in ['write_lat_ms','write_clat_ms','write_p99_ms','write_iops_stdev']:
                lat_sums[k] += m[k]
            cnt_write += 1

    def avg(key, cnt): return lat_sums[key] / cnt if cnt else 0.0

    # ── Table 1: Throughput ───────────────────────────────────────────────
    th_headers = ['Pod Name','R-IOPS','W-IOPS','R-IOPS σ','W-IOPS σ','R-BW MB/s','W-BW MB/s']
    th_widths  = [28, 9, 9, 10, 10, 12, 12]

    def fmt_throughput(row, w):
        pod, m = row
        return (f'{pod:<{w[0]}} | {m["read_iops"]:<{w[1]}.0f} | {m["write_iops"]:<{w[2]}.0f} | '
                f'{m["read_iops_stdev"]:<{w[3]}.1f} | {m["write_iops_stdev"]:<{w[4]}.1f} | '
                f'{m["read_bw_mbps"]:<{w[5]}.3f} | {m["write_bw_mbps"]:<{w[6]}.3f}')

    print("=== THROUGHPUT ===")
    for line in make_table(rows, th_headers, th_widths, fmt_throughput):
        print(line)
    # Summary row
    sep = '-' * (sum(th_widths) + 3 * (len(th_widths)-1))
    print(f'{"TOTAL":<{th_widths[0]}} | {totals["read_iops"]:<{th_widths[1]}.0f} | {totals["write_iops"]:<{th_widths[2]}.0f} | '
          f'{avg("read_iops_stdev",cnt_read):<{th_widths[3]}.1f} | {avg("write_iops_stdev",cnt_write):<{th_widths[4]}.1f} | '
          f'{totals["read_bw_mbps"]:<{th_widths[5]}.3f} | {totals["write_bw_mbps"]:<{th_widths[6]}.3f}')
    print(sep)

    # ── Table 2: Latency ──────────────────────────────────────────────────
    lat_headers = ['Pod Name','R-lat ms','W-lat ms','R-clat ms','W-clat ms','R-P99 ms','W-P99 ms']
    lat_widths  = [28, 10, 10, 11, 11, 10, 10]

    def fmt_latency(row, w):
        pod, m = row
        return (f'{pod:<{w[0]}} | {m["read_lat_ms"]:<{w[1]}.3f} | {m["write_lat_ms"]:<{w[2]}.3f} | '
                f'{m["read_clat_ms"]:<{w[3]}.3f} | {m["write_clat_ms"]:<{w[4]}.3f} | '
                f'{m["read_p99_ms"]:<{w[5]}.3f} | {m["write_p99_ms"]:<{w[6]}.3f}')

    print("\n=== LATENCY ===")
    for line in make_table(rows, lat_headers, lat_widths, fmt_latency):
        print(line)
    # Average row
    sep2 = '-' * (sum(lat_widths) + 3 * (len(lat_widths)-1))
    print(f'{"AVERAGE":<{lat_widths[0]}} | {avg("read_lat_ms",cnt_read):<{lat_widths[1]}.3f} | {avg("write_lat_ms",cnt_write):<{lat_widths[2]}.3f} | '
          f'{avg("read_clat_ms",cnt_read):<{lat_widths[3]}.3f} | {avg("write_clat_ms",cnt_write):<{lat_widths[4]}.3f} | '
          f'{avg("read_p99_ms",cnt_read):<{lat_widths[5]}.3f} | {avg("write_p99_ms",cnt_write):<{lat_widths[6]}.3f}')
    print(sep2)

    # ── CSV Export ────────────────────────────────────────────────────────
    csv_file = os.path.join(results_dir, "summary_report.csv")
    csv_cols = [
        'Pod Name',
        'Read IOPS','Write IOPS','Read IOPS Stddev','Write IOPS Stddev',
        'Read BW (MB/s)','Write BW (MB/s)',
        'Read Lat Avg (ms)','Write Lat Avg (ms)',
        'Read Clat Avg (ms)','Write Clat Avg (ms)',
        'Read P99 (ms)','Write P99 (ms)',
    ]
    with open(csv_file, 'w', newline='') as csvfile:
        w = csv.writer(csvfile)
        w.writerow(csv_cols)
        for pod, m in rows:
            w.writerow([
                pod,
                round(m['read_iops']),       round(m['write_iops']),
                round(m['read_iops_stdev'],2), round(m['write_iops_stdev'],2),
                round(m['read_bw_mbps'],3),   round(m['write_bw_mbps'],3),
                round(m['read_lat_ms'],3),    round(m['write_lat_ms'],3),
                round(m['read_clat_ms'],3),   round(m['write_clat_ms'],3),
                round(m['read_p99_ms'],3),    round(m['write_p99_ms'],3),
            ])
        w.writerow([
            'TOTAL / AVG',
            round(totals['read_iops']),       round(totals['write_iops']),
            round(avg('read_iops_stdev',cnt_read),2), round(avg('write_iops_stdev',cnt_write),2),
            round(totals['read_bw_mbps'],3),  round(totals['write_bw_mbps'],3),
            round(avg('read_lat_ms',cnt_read),3),   round(avg('write_lat_ms',cnt_write),3),
            round(avg('read_clat_ms',cnt_read),3),  round(avg('write_clat_ms',cnt_write),3),
            round(avg('read_p99_ms',cnt_read),3),   round(avg('write_p99_ms',cnt_write),3),
        ])

    print(f"\nCSV saved to: {csv_file}")


if __name__ == "__main__":
    main()
