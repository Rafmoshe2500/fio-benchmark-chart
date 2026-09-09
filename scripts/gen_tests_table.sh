#!/bin/bash
# Regenerate the test table in jobs/tests/README.md from the actual .fio and
# .meta.json files, so the documentation cannot drift from the tests.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
python3 - <<'PY'
import json, glob, os, sys
sys.path.insert(0, ".")
from lib.fiojob import parse_job_file

hdr = ["test", "pods", "bs", "pattern", "r/w", "target per pod",
       "rate line", "runtime", "dataset/pod"]
print("| " + " | ".join(hdr) + " |")
print("|" + "|".join(["---"] * len(hdr)) + "|")
for f in sorted(glob.glob("../jobs/tests/*.fio")):
    tid = os.path.basename(f)[:-4]
    s = parse_job_file(f)
    m = json.load(open("../jobs/tests/%s.meta.json" % tid))
    rate = s.option("rate_iops") or s.option("rate") or "-"
    if len(s.sections) > 1:
        rate = "`%s` x%d sections" % (rate, len(s.sections))
    else:
        rate = "`%s`" % rate if rate != "-" else "-"
    if m["target_iops_per_pod"]:
        target = "{:,} IOPS".format(m["target_iops_per_pod"])
    elif m["target_bw_mibps_per_pod"]:
        target = "{:,} MiB/s".format(m["target_bw_mibps_per_pod"])
    else:
        target = "uncapped"
    pods = "step" if "gradual_scale" in tid else str(m["replicas"])
    print("| `%s` | %s | %s | %s | %d/%d | %s | %s | %ss | %d GiB |" % (
        tid, pods, s.option("bs", "-"), m["pattern"],
        m["rw_mix_read"] * 100, (1 - m["rw_mix_read"]) * 100,
        target, rate, m["runtime_s"], m["dataset_gib_per_pod"]))
PY
