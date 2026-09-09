#!/usr/bin/env python3
"""Read NFS RPC statistics out of /proc/self/mountstats.

This is the only NFS latency source that costs nothing and needs no access to
the array: the client kernel already counts every RPC, its retransmissions,
its timeouts and its cumulative round-trip time, per operation. Two snapshots
around a measurement window give the mean RTT per op for that window.

It is also the one place a "network retry" claim can be supported by
evidence. retrans_pct comes from ntrans exceeding ops, which means the client
genuinely resent RPCs. Nothing in fio's or NiFi's output can establish that.

Per-op columns (statvers=1.1), in order:
    ops ntrans timeouts bytes_sent bytes_recv queue_ms rtt_ms execute_ms

Usage:
    nfsstat.py                                  # JSON snapshot to stdout
    nfsstat.py --before before.json             # human-readable delta
    nfsstat.py --before before.json --mount /x  # one mount only
"""

import argparse
import json
import re
import sys

_DEVICE = re.compile(r"^device (\S+) mounted on (\S+) with fstype (\S+)")
_OPTS = re.compile(r"vers=([\d.]+)")
_FIELDS = ("ops", "ntrans", "timeouts", "bytes_sent", "bytes_recv",
           "queue_ms", "rtt_ms", "execute_ms")


def parse_mountstats(text):
    """Return {mountpoint: {"device":..., "vers":..., "ops": {OP: {...}}}}."""
    mounts, current = {}, None
    for line in text.splitlines():
        hit = _DEVICE.match(line)
        if hit:
            device, mountpoint, fstype = hit.groups()
            if not fstype.startswith("nfs"):
                current = None
                continue
            current = {"device": device, "vers": None, "ops": {}}
            mounts[mountpoint] = current
            continue
        if current is None:
            continue
        if "opts:" in line:
            v = _OPTS.search(line)
            if v:
                current["vers"] = v.group(1)
            continue
        stripped = line.strip()
        if ":" not in stripped:
            continue
        name, _, rest = stripped.partition(":")
        parts = rest.split()
        if len(parts) < len(_FIELDS) or not parts[0].isdigit():
            continue
        current["ops"][name.strip()] = {
            k: int(v) for k, v in zip(_FIELDS, parts[:len(_FIELDS)])
        }
    return mounts


def delta(before, after, mountpoint):
    """Per-op deltas plus the derived means for the window.

    mean_rtt_ms is None when no operations happened. Dividing by zero ops and
    reporting 0.00 ms would read as "instant" when it means "idle", and that
    distinction is exactly what a storage benchmark must not blur.
    """
    a = (before.get(mountpoint) or {}).get("ops", {})
    b = (after.get(mountpoint) or {}).get("ops", {})
    out = {}
    for op in sorted(set(a) | set(b)):
        pa, pb = a.get(op, {}), b.get(op, {})
        d = {k: pb.get(k, 0) - pa.get(k, 0) for k in _FIELDS}
        ops = d["ops"]
        d["mean_rtt_ms"] = (d["rtt_ms"] / ops) if ops > 0 else None
        d["mean_queue_ms"] = (d["queue_ms"] / ops) if ops > 0 else None
        d["mean_exec_ms"] = (d["execute_ms"] / ops) if ops > 0 else None
        d["retrans_pct"] = (100.0 * (d["ntrans"] - ops) / ops) if ops > 0 else None
        out[op] = d
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="/proc/self/mountstats")
    ap.add_argument("--before", help="JSON snapshot to diff against")
    ap.add_argument("--mount", default="")
    a = ap.parse_args()

    with open(a.file, errors="replace") as fh:
        now = parse_mountstats(fh.read())

    if not a.before:
        json.dump(now, sys.stdout)
        return 0

    with open(a.before) as fh:
        before = json.load(fh)

    mounts = [a.mount] if a.mount else sorted(now)
    for m in mounts:
        vers = (now.get(m) or {}).get("vers", "?")
        print("=== %s (NFSv%s) ===" % (m, vers))
        rows = delta(before, now, m)
        busy = [(op, d) for op, d in rows.items() if d["ops"]]
        if not busy:
            print("  no NFS operations during the window")
            continue
        for op, d in busy:
            print("  %-12s %10s ops  rtt %8.2f ms  queue %7.2f ms  "
                  "retrans %5.2f%%  timeouts %6s"
                  % (op, format(d["ops"], ","), d["mean_rtt_ms"],
                     d["mean_queue_ms"], d["retrans_pct"],
                     format(d["timeouts"], ",")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
