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


def main():
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["build", "state", "stats", "clear", "ping"])
    p.add_argument("--url", required=True)
    p.add_argument("--user", default="admin")
    p.add_argument("--password", required=True)
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
    elif a.action == "clear":
        n.clear()


if __name__ == "__main__":
    try:
        main()
    except NifiError as e:
        raise SystemExit(f"nifi api error: {e}")
