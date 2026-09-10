"""Parse a fio job file well enough to know what it will demand of the
storage before we ask a cluster for it.

The one thing this module exists to get right: `size` is per clone, and the
number of clones is `numjobs` applied to every section. Getting that wrong is
what let a 10Gi PVC accept a job needing 80GiB, which died with ENOSPC ten
minutes into results/test4_10pods_50k_5050_32kb.
"""

import re

_SUFFIX = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4}

_READ_PATTERNS = {"read", "randread"}
_WRITE_PATTERNS = {"write", "randwrite"}
_MIXED_PATTERNS = {"rw", "randrw", "readwrite", "trimwrite"}


def parse_size(text):
    """fio sizes are binary by default: 10G is 10 GiB, not 10 GB."""
    t = str(text).strip().lower().rstrip("b")
    if not t:
        return 0
    if t[-1] in _SUFFIX:
        return int(float(t[:-1]) * _SUFFIX[t[-1]])
    return int(float(t))


class JobSpec:
    """Plain class rather than a dataclass on purpose: dataclasses arrived in
    Python 3.7, and RHEL 8 -- a common OpenShift jump host -- ships 3.6 as its
    default python3. Keeping this importable there avoids requiring a newer
    interpreter on a machine that may have no package repository."""

    def __init__(self, path, numjobs=1, size_bytes=0, directions=None,
                 sections=None, job_options=None, globals=None):
        self.path = path
        self.numjobs = numjobs
        self.size_bytes = size_bytes
        self.directions = set() if directions is None else directions
        self.sections = [] if sections is None else sections
        self.job_options = [] if job_options is None else job_options
        self.globals = {} if globals is None else globals

    @property
    def required_bytes(self):
        return self.size_bytes * self.numjobs

    def required_gib(self, headroom=1.2):
        return self.required_bytes * headroom / 1024 ** 3

    def option(self, key, default=None):
        """Resolve an option from [global], falling back to the first job
        section. bs and rw live in the section in most of these files."""
        if key in self.globals:
            return self.globals[key]
        for opts in self.job_options:
            if key in opts:
                return opts[key]
        return default


def _directions_for(rw):
    r = (rw or "").strip().lower()
    if r in _READ_PATTERNS:
        return {"read"}
    if r in _WRITE_PATTERNS:
        return {"write"}
    if r in _MIXED_PATTERNS:
        return {"read", "write"}
    return set()


def parse_job_file(path):
    """Return a JobSpec. Inline `;` and `#` comments are stripped, which the
    real job files use -- test7's [global] block is annotated that way."""
    spec = JobSpec(path=path)
    current = None
    sections = []

    with open(path, "r", errors="replace") as fh:
        for raw in fh:
            line = re.split(r"[;#]", raw, maxsplit=1)[0].strip()
            if not line:
                continue
            if line.startswith("[") and line.endswith("]"):
                current = {"name": line[1:-1]}
                sections.append(current)
                continue
            if "=" not in line or current is None:
                continue
            key, _, value = line.partition("=")
            current[key.strip().lower()] = value.strip()

    glb = next((s for s in sections if s["name"] == "global"), {})
    jobs = [s for s in sections if s["name"] != "global"]

    spec.globals = {k: v for k, v in glb.items() if k != "name"}
    spec.sections = [s["name"] for s in jobs]
    spec.job_options = [{k: v for k, v in s.items() if k != "name"} for s in jobs]
    spec.size_bytes = parse_size(glb.get("size", "0"))

    # numjobs from [global] applies to every section. Without it each section
    # is one clone, so test2's eight sections are eight clones.
    if "numjobs" in glb:
        spec.numjobs = int(glb["numjobs"]) * max(len(jobs), 1)
    else:
        spec.numjobs = sum(int(s.get("numjobs", 1)) for s in jobs) or 1

    for s in jobs:
        spec.directions |= _directions_for(s.get("rw", glb.get("rw", "")))
    if not spec.directions:
        spec.directions = _directions_for(glb.get("rw", ""))

    return spec
