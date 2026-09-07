#!/usr/bin/env python3
"""
nififlow.py -- drive an Apache NiFi 2.x node over its REST API.

Used by nifi-nfs-loadtest.sh, but works standalone against any reachable
NiFi node. Standard library only, no pip install needed.

  build  -- create the synthetic load flow in the root process group
  state  -- set the root process group RUNNING / STOPPED
  stats  -- one-shot throughput + repository utilisation snapshot
  clear  -- delete every processor/connection in the root process group
"""

import argparse
import json
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

# We always talk to a local kubectl port-forward. An https_proxy / HTTPS_PROXY
# in the environment would otherwise be used for 127.0.0.1 too, which fails
# with an opaque URLError. An empty ProxyHandler disables proxy lookup for
# this opener regardless of what is exported in the shell.
OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    urllib.request.HTTPSHandler(context=CTX),
)


class NifiError(Exception):
    pass

GEN = "org.apache.nifi.processors.standard.GenerateFlowFile"
PUT = "org.apache.nifi.processors.standard.PutFile"
RPL = "org.apache.nifi.processors.standard.ReplaceText"
UPD = "org.apache.nifi.processors.attributes.UpdateAttribute"


def _norm(s):
    """Collapse a property or value name to something comparable across
    NiFi versions: lowercase, alphanumerics only."""
    return "".join(c for c in str(s).lower() if c.isalnum())


def _allowable(descriptor):
    vals = descriptor.get("allowableValues") or []
    out = []
    for v in vals:
        av = v.get("allowableValue") or v
        val = av.get("value")
        if val is not None:
            out.append(val)
    return out


def _match(want, descriptors):
    """Find the descriptor key for a wanted property name, matching on the
    canonical name or the display name, ignoring case and punctuation."""
    target = _norm(want)
    for key, d in descriptors.items():
        for cand in (key, d.get("name"), d.get("displayName")):
            if cand and _norm(cand) == target:
                return key
    # last resort: a descriptor whose normalised name contains the target
    for key, d in descriptors.items():
        for cand in (key, d.get("name"), d.get("displayName")):
            if cand and target and target in _norm(cand):
                return key
    return None


class Nifi:
    def __init__(self, base, user, password, wait=0):
        self.base = base.rstrip("/") + "/nifi-api"
        # NiFi opens its port well before the REST API is usable, and the
        # startup probe is only a TCP check, so retry rather than fail on
        # the first refusal.
        deadline = time.time() + max(wait, 0)
        attempt, last = 0, None
        while True:
            attempt += 1
            try:
                self.token = self._token(user, password)
                if attempt > 1:
                    print(f"  connected after {attempt} attempts", file=sys.stderr)
                return
            except NifiError as e:
                last = e
                if time.time() >= deadline:
                    raise SystemExit(
                        f"{e}\n"
                        "  checked: is the port-forward alive, and has NiFi finished starting?\n"
                        "    kubectl -n <ns> exec nifi-0 -c nifi -- "
                        "tail -20 /opt/nifi/nifi-current/logs/nifi-app.log\n"
                        "  proxy variables are already bypassed for this connection."
                    )
                time.sleep(5)

    def _raw(self, method, path, body=None, headers=None, raw_body=None):
        url = self.base + path
        data = None
        hdrs = dict(headers or {})
        if raw_body is not None:
            data = raw_body.encode()
        elif body is not None:
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        try:
            with OPENER.open(req, timeout=30) as r:
                return r.read().decode()
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            raise NifiError(f"{method} {path} -> HTTP {e.code}: {detail}")
        except urllib.error.URLError as e:
            raise NifiError(f"cannot reach {url}: {e.reason}")
        except (ssl.SSLError, OSError) as e:
            raise NifiError(f"cannot reach {url}: {e}")

    def _token(self, user, password):
        form = urllib.parse.urlencode({"username": user, "password": password})
        return self._raw(
            "POST", "/access/token",
            raw_body=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ).strip()

    def call(self, method, path, body=None):
        out = self._raw(method, path, body,
                        headers={"Authorization": "Bearer " + self.token})
        return json.loads(out) if out.strip() else {}

    # ---------------- flow construction ----------------

    def processor(self, ptype, name, x, y, props, autoterm, threads, schedule):
        """Create a processor, then resolve the requested property names
        against the descriptors this NiFi build actually exposes before
        applying them. Property display names drift between NiFi versions;
        this makes the flow build version-independent instead of guessing."""
        created = self.call("POST", "/process-groups/root/processors", {
            "revision": {"version": 0},
            "component": {"type": ptype, "name": name,
                          "position": {"x": float(x), "y": float(y)}},
        })
        pid = created["id"]

        ent = self.call("GET", f"/processors/{pid}")
        cfg = ent["component"]["config"]
        descriptors = cfg.get("descriptors", {}) or {}
        relationships = [r["name"] for r in ent["component"].get("relationships", [])]

        resolved, warnings = {}, []
        for want, value in props.items():
            key = _match(want, descriptors)
            if key is None:
                warnings.append(f"{name}: no property matching {want!r}; "
                                f"available: {', '.join(sorted(descriptors)) or 'none'}")
                continue
            allowed = _allowable(descriptors[key])
            if allowed:
                hit = next((a for a in allowed if _norm(a) == _norm(str(value))), None)
                if hit is None:
                    warnings.append(f"{name}: {value!r} is not valid for {key!r}; "
                                    f"allowed: {', '.join(allowed)}")
                    continue
                value = hit
            resolved[key] = str(value)

        term = [r for r in autoterm
                if r in relationships] or [r for r in relationships
                                           if _norm(r) in {_norm(a) for a in autoterm}]

        self.call("PUT", f"/processors/{pid}", {
            "revision": ent["revision"],
            "component": {
                "id": pid,
                "config": {
                    "properties": resolved,
                    "schedulingPeriod": schedule,
                    "schedulingStrategy": "TIMER_DRIVEN",
                    "concurrentlySchedulableTaskCount": str(threads),
                    "autoTerminatedRelationships": term,
                    "penaltyDuration": "30 sec",
                    "yieldDuration": "1 sec",
                },
            },
        })
        for w in warnings:
            print("  warn: " + w, file=sys.stderr)
        return pid

    def relationships(self, pid):
        ent = self.call("GET", f"/processors/{pid}")
        return [r["name"] for r in ent["component"].get("relationships", [])]

    def connect(self, src, dst, rel, obj_threshold, size_threshold):
        body = {
            "revision": {"version": 0},
            "component": {
                "source": {"id": src, "groupId": "root", "type": "PROCESSOR"},
                "destination": {"id": dst, "groupId": "root", "type": "PROCESSOR"},
                "selectedRelationships": [rel],
                "backPressureObjectThreshold": str(obj_threshold),
                "backPressureDataSizeThreshold": size_threshold,
            },
        }
        self.call("POST", "/process-groups/root/connections", body)

    def clear(self):
        for c in self.call("GET", "/process-groups/root/connections").get("connections", []):
            self.call("DELETE",
                      f"/connections/{c['id']}?version={c['revision']['version']}"
                      f"&clientId={c['revision'].get('clientId','')}")
        for p in self.call("GET", "/process-groups/root/processors").get("processors", []):
            self.call("DELETE",
                      f"/processors/{p['id']}?version={p['revision']['version']}"
                      f"&clientId={p['revision'].get('clientId','')}")

    def set_state(self, state):
        self.call("PUT", "/flow/process-groups/root", {"id": "root", "state": state})

    def invalid(self):
        out = []
        for p in self.call("GET", "/process-groups/root/processors").get("processors", []):
            c = p["component"]
            if c.get("validationStatus") == "INVALID":
                out.append((c["name"], c.get("validationErrors") or []))
        return out


def _success_of(n, pid):
    """Pick the success-like outgoing relationship of a processor."""
    rels = n.relationships(pid)
    for r in rels:
        if _norm(r) == "success":
            return r
    return rels[0] if rels else "success"


def build(n, a):
    gen = n.processor(
        GEN, "LOAD-Generate", 0, 0,
        {"File Size": a.file_size, "Batch Size": str(a.batch),
         "Data Format": "Binary", "Unique FlowFiles": "true"},
        [], a.threads, a.schedule)

    prev, x = gen, 400
    for i in range(1, a.rewrites + 1):
        rid = n.processor(
            RPL, f"LOAD-Rewrite-{i}", x, 0,
            # Prepend (not Always Replace): the full original payload is
            # read and re-written into a brand new content claim on every
            # hop, at full size. That is the churn we want to measure.
            {"Replacement Strategy": "Prepend",
             "Replacement Value": f"churn-pass-{i}\n",
             "Evaluation Mode": "Entire text",
             "Maximum Buffer Size": "64 MB"},
            ["failure"], a.threads, "0 sec")
        n.connect(prev, rid, _success_of(n, prev), a.bp_objects, a.bp_size)
        prev, x = rid, x + 400

    if a.output_dir:
        pid = n.processor(
            PUT, "LOAD-PutFile", x, 0,
            {"Directory": a.output_dir,
             "Conflict Resolution Strategy": "replace",
             "Create Missing Directories": "true"},
            ["success", "failure"], a.threads, "0 sec")
    else:
        pid = n.processor(UPD, "LOAD-Sink", x, 0, {}, ["success"], a.threads, "0 sec")
    n.connect(prev, pid, _success_of(n, prev), a.bp_objects, a.bp_size)

    bad = n.invalid()
    if bad:
        print("  INVALID processors (property names may differ on your NiFi build):",
              file=sys.stderr)
        for name, errs in bad:
            print(f"    {name}: {'; '.join(errs)}", file=sys.stderr)
        return 1
    return 0


def _gb(b):
    return b / (1024 ** 3)


CSV_COLS = ["ts", "node", "bytes_read", "bytes_written", "bytes_in", "bytes_out",
            "ff_in", "ff_out", "queued_count", "queued_bytes",
            "ffrepo_used", "content_used", "prov_used", "heap_used", "gc_ms"]


def _snapshot(n):
    """Raw numbers for one node. NiFi's throughput counters cover a rolling
    window (5 min by default), so they are rate indicators, not totals. The
    repository usage figures are absolute, so their delta over a run is the
    honest measure of how many bytes actually landed."""
    agg = n.call("GET", "/flow/process-groups/root/status?recursive=true")[
        "processGroupStatus"]["aggregateSnapshot"]
    sd = n.call("GET", "/system-diagnostics")["systemDiagnostics"]["aggregateSnapshot"]

    def used(x):
        if isinstance(x, list):
            x = x[0] if x else {}
        return (x or {}).get("usedSpaceBytes", 0)

    gc_ms = sum(g.get("collectionMillis", 0) or 0
                for g in (sd.get("garbageCollection") or []))
    return {
        "ts": int(time.time()),
        "bytes_read": agg.get("bytesRead", 0),
        "bytes_written": agg.get("bytesWritten", 0),
        "bytes_in": agg.get("bytesIn", 0),
        "bytes_out": agg.get("bytesOut", 0),
        "ff_in": agg.get("flowFilesIn", 0),
        "ff_out": agg.get("flowFilesOut", 0),
        "queued_count": agg.get("flowFilesQueued", 0),
        "queued_bytes": agg.get("bytesQueued", 0),
        "ffrepo_used": used(sd.get("flowFileRepositoryStorageUsage")),
        "content_used": used(sd.get("contentRepositoryStorageUsage")),
        "prov_used": used(sd.get("provenanceRepositoryStorageUsage")),
        "heap_used": sd.get("usedHeapBytes", 0),
        "gc_ms": gc_ms,
    }


def sample(n, label):
    r = _snapshot(n)
    r["node"] = label
    print(",".join(str(r.get(c, "")) for c in CSV_COLS))


def stats(n, label):
    st = n.call("GET", "/flow/process-groups/root/status?recursive=true")
    agg = st["processGroupStatus"]["aggregateSnapshot"]
    sd = n.call("GET", "/system-diagnostics")["systemDiagnostics"]["aggregateSnapshot"]

    def repo(r):
        if not r:
            return "     n/a"
        used = r.get("usedSpaceBytes", 0)
        total = r.get("totalSpaceBytes", 0) or 1
        return f"{_gb(used):6.2f}G/{100*used/total:4.1f}%"

    ff = sd.get("flowFileRepositoryStorageUsage") or {}
    cr = (sd.get("contentRepositoryStorageUsage") or [{}])[0]
    pr = (sd.get("provenanceRepositoryStorageUsage") or [{}])[0]

    print(f"{label:8s} in={agg.get('read','-'):>11}  out={agg.get('written','-'):>11}  "
          f"queued={agg.get('queued','-'):>15}")
    print(f"         ff-repo {repo(ff)}   content {repo(cr)}   provenance {repo(pr)}")


def _fmt(b):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if abs(b) < 1024 or u == "TB":
            return f"{b:,.1f} {u}"
        b /= 1024


def _slope(xs, ys):
    """Least-squares slope, units of y per unit of x."""
    n = len(xs)
    if n < 2:
        return 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den


def _pct(vals, q):
    if not vals:
        return 0
    v = sorted(vals)
    i = min(int(round(q * (len(v) - 1))), len(v) - 1)
    return v[i]


def summarize(path):
    """Turn a recorded CSV into something you can put in a report."""
    import csv as _csv
    from collections import defaultdict

    if not path:
        raise SystemExit("summarize needs --csv <file>")
    rows = defaultdict(list)
    with open(path) as fh:
        for r in _csv.DictReader(fh):
            try:
                rows[r["node"]].append({k: int(v) for k, v in r.items() if k != "node"})
            except (ValueError, TypeError):
                continue
    if not rows:
        raise SystemExit(f"no usable samples in {path}")

    all_ts = [s["ts"] for v in rows.values() for s in v]
    elapsed = max(all_ts) - min(all_ts) or 1

    print("=" * 74)
    print(f"NiFi storage load test summary   {path}")
    print(f"duration {elapsed//60}m {elapsed%60}s across {len(rows)} node(s), "
          f"{sum(len(v) for v in rows.values())} samples")
    print("=" * 74)

    tot_content = tot_prov = tot_ff = 0
    for node in sorted(rows):
        s = sorted(rows[node], key=lambda x: x["ts"])
        first, last = s[0], s[-1]
        span = (last["ts"] - first["ts"]) or 1

        # Absolute repository growth: the trustworthy throughput measure.
        d_content = last["content_used"] - first["content_used"]
        d_prov = last["prov_used"] - first["prov_used"]
        d_ff = last["ffrepo_used"] - first["ffrepo_used"]
        tot_content += d_content; tot_prov += d_prov; tot_ff += d_ff

        # Per-interval write rates derived from absolute repo growth. This
        # avoids NiFi's rolling-window counters entirely, so the numbers are
        # directly comparable to what the array reports.
        rates = []
        for a_, b_ in zip(s, s[1:]):
            dt = b_["ts"] - a_["ts"]
            if dt > 0:
                rates.append(max(b_["content_used"] - a_["content_used"], 0) / dt)

        q = [x["queued_count"] for x in s]
        ts = [x["ts"] - first["ts"] for x in s]
        gc = last["gc_ms"] - first["gc_ms"]
        qslope = _slope(ts, q)

        print(f"\n{node}")
        print(f"  content repo   +{_fmt(d_content):>12}   "
              f"mean {_fmt(d_content/span):>11}/s")
        print(f"  provenance     +{_fmt(d_prov):>12}   "
              f"mean {_fmt(d_prov/span):>11}/s")
        print(f"  flowfile repo  +{_fmt(d_ff):>12}")
        if rates:
            print(f"  write rate      median {_fmt(_pct(rates,.5)):>11}/s   "
                  f"p95 {_fmt(_pct(rates,.95)):>11}/s   "
                  f"max {_fmt(max(rates)):>11}/s")
        print(f"  queue depth     start {q[0]:>8,}   end {q[-1]:>8,}   "
              f"peak {max(q):>8,}   trend {qslope:+.2f}/s")

        # Trend, not endpoints: a random walk of a few dozen FlowFiles is
        # noise, a sustained slope is storage failing to keep up.
        if qslope > 0.5 and q[-1] > q[0] + 500:
            verdict = "CLIMBING - storage did not keep up with the generator"
        elif qslope < -0.5 and q[-1] < q[0]:
            verdict = "DRAINING - backlog clearing, storage ahead of generator"
        else:
            verdict = "STABLE - generator and storage in balance"
        print(f"  verdict         {verdict}")
        print(f"  GC time         {gc/1000:.1f}s over the run "
              f"({100*gc/(span*1000):.1f}% of wall clock)")

    print("\n" + "-" * 74)
    print(f"cluster total    content +{_fmt(tot_content)}   "
          f"provenance +{_fmt(tot_prov)}   flowfile +{_fmt(tot_ff)}")
    print(f"aggregate write rate to content repos: "
          f"{_fmt(tot_content/elapsed)}/s")
    print("-" * 74)
    print("write rates above are derived from absolute repository growth, not from")
    print("NiFi's rolling-window counters, so they can be compared directly with")
    print("your array's own metrics. The raw CSV keeps both.")


def _load_csv(path):
    import csv as _csv
    from collections import defaultdict
    rows = defaultdict(list)
    with open(path) as fh:
        for r in _csv.DictReader(fh):
            try:
                rows[r["node"]].append({k: int(v) for k, v in r.items() if k != "node"})
            except (ValueError, TypeError):
                continue
    return rows


def _deployment_stats(path):
    """Collapse one deployment's CSV into a single row of headline figures."""
    rows = _load_csv(path)
    if not rows:
        return None
    ts = [x["ts"] for v in rows.values() for x in v]
    elapsed = (max(ts) - min(ts)) or 1

    content = prov = ffrepo = 0
    q_end = q_peak = 0
    worst_slope = None
    for node, samples in rows.items():
        s = sorted(samples, key=lambda x: x["ts"])
        content += s[-1]["content_used"] - s[0]["content_used"]
        prov += s[-1]["prov_used"] - s[0]["prov_used"]
        ffrepo += s[-1]["ffrepo_used"] - s[0]["ffrepo_used"]
        q = [x["queued_count"] for x in s]
        q_end += q[-1]; q_peak = max(q_peak, max(q))
        sl = _slope([x["ts"] - s[0]["ts"] for x in s], q)
        worst_slope = sl if worst_slope is None else max(worst_slope, sl)

    rates = []
    for node, samples in rows.items():
        s = sorted(samples, key=lambda x: x["ts"])
        for a_, b_ in zip(s, s[1:]):
            dt = b_["ts"] - a_["ts"]
            if dt > 0:
                rates.append(max(b_["content_used"] - a_["content_used"], 0) / dt)

    return {
        "nodes": len(rows), "elapsed": elapsed,
        "content": content, "prov": prov, "ffrepo": ffrepo,
        "mean_rate": content / elapsed,
        "p95_rate": _pct(rates, .95) * len(rows) if rates else 0,
        "q_end": q_end, "q_peak": q_peak,
        "slope": worst_slope or 0.0,
    }


def compare(paths):
    import os
    results = []
    for p_ in paths:
        st = _deployment_stats(p_)
        if st is None:
            print(f"  (skipping {p_}: no usable samples)", file=sys.stderr)
            continue
        label = os.path.basename(p_).split("-")[0]
        results.append((label, p_, st))
    if not results:
        raise SystemExit("nothing to compare")

    w = 78
    print("=" * w)
    print("Deployment comparison")
    print("=" * w)
    hdr = f"{'':12} {'nodes':>5} {'duration':>9} {'content written':>16} {'mean rate':>13}"
    print(hdr)
    for label, _, st in results:
        print(f"{label:12} {st['nodes']:>5} "
              f"{st['elapsed']//60:>6}m{st['elapsed']%60:02d}s "
              f"{_fmt(st['content']):>16} {_fmt(st['mean_rate'])+'/s':>13}")

    print()
    print(f"{'':12} {'p95 rate':>13} {'provenance':>13} {'queue peak':>11} "
          f"{'trend':>9}  verdict")
    for label, _, st in results:
        if st["slope"] > 0.5:
            v = "did not keep up"
        elif st["slope"] < -0.5:
            v = "draining"
        else:
            v = "kept up"
        print(f"{label:12} {_fmt(st['p95_rate'])+'/s':>13} "
              f"{_fmt(st['prov']):>13} {st['q_peak']:>11,} "
              f"{st['slope']:>+8.2f}/s  {v}")

    # Relative comparison only makes sense with a common baseline.
    if len(results) > 1:
        base_label, _, base = max(results, key=lambda r: r[2]["mean_rate"])
        print()
        print(f"relative to {base_label} (highest sustained rate):")
        for label, _, st in results:
            if label == base_label:
                continue
            d = 100 * (st["mean_rate"] - base["mean_rate"]) / (base["mean_rate"] or 1)
            print(f"  {label:12} {d:+6.1f}% sustained write rate")

    print()
    print("Durations and start times differ slightly between deployments; treat")
    print("small gaps as noise. Runs recorded concurrently share cluster CPU and")
    print("network, so a slower deployment may be client-bound rather than")
    print("array-bound. Cross-check against per-node CPU before concluding.")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("action",
                   choices=["build", "state", "stats", "clear", "ping",
                            "sample", "header", "summarize", "compare"])
    p.add_argument("--url", default="")
    p.add_argument("--csv", action="append", default=[],
                   help="CSV path; repeat for compare")
    p.add_argument("--user", default="admin")
    p.add_argument("--password", default="")
    p.add_argument("--label", default="node")
    p.add_argument("--wait", type=int, default=0,
                   help="seconds to keep retrying the initial connection")
    p.add_argument("--file-size", default="4 KB")
    p.add_argument("--batch", type=int, default=200)
    p.add_argument("--threads", type=int, default=8)
    p.add_argument("--rewrites", type=int, default=0)
    p.add_argument("--schedule", default="0 sec")
    p.add_argument("--output-dir", default="")
    p.add_argument("--bp-objects", type=int, default=20000)
    p.add_argument("--bp-size", default="4 GB")
    p.add_argument("--state", default="RUNNING", choices=["RUNNING", "STOPPED"])
    a = p.parse_args()

    # these two need no cluster connection
    if a.action == "header":
        print(",".join(CSV_COLS))
        return
    if a.action == "summarize":
        summarize(a.csv[0] if a.csv else "")
        return
    if a.action == "compare":
        compare(a.csv)
        return
    if not a.url or not a.password:
        raise SystemExit("--url and --password are required for this action")

    n = Nifi(a.url, a.user, a.password, wait=a.wait)
    if a.action == "ping":
        print(f"{a.label}: reachable, authenticated")
        return
    if a.action == "build":
        sys.exit(build(n, a))
    elif a.action == "state":
        n.set_state(a.state)
    elif a.action == "stats":
        stats(n, a.label)
    elif a.action == "sample":
        sample(n, a.label)
    elif a.action == "clear":
        n.clear()


if __name__ == "__main__":
    try:
        main()
    except NifiError as e:
        raise SystemExit(f"nifi api error: {e}")
