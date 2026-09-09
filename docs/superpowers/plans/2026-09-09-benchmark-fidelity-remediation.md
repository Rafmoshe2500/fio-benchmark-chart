# תוכנית תיקון: אמינות בדיקות FIO + NiFi

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** להפוך את חבילת הבדיקות מ־exploratory load generator לכלי benchmark שניתן להסתמך עליו להחלטות קיבולת והשוואת אחסון — כך שכל מספר שהיא מדווחת מייצג עבודה אמיתית שהתרחשה, וכל ריצה פגומה נפסלת במקום להתדרדר בשקט ל־PASS.

**Architecture:** שלושה שינויים חוצי־מערכת: (1) כל משאב Kubernetes נושא `run-id` ייחודי, כך שאיסוף וניקוי הם scoped ולא name-prefix guessing; (2) fio מדווח `json+` עם time-series במקום טקסט, ו־parser יחיד עם סכמה קשיחה פוסל ריצות חסרות; (3) NiFi מודד עבודה מצטברת (counters + output accounting + NFS RPC stats) במקום גידול נטו בנפח repository.

**Tech Stack:** Helm 3, Kubernetes/OpenShift, fio 3.41, Python 3.9+ (stdlib בלבד), Bash 4+, Apache NiFi 2.11 REST API.

---

## ממצאי אימות — מה נבדק מול הקוד

הסקירה שב־Drive נקראה ואומתה מול הקוד ב־`a2e459d` (ה־commit שהיא מציינת, `ed845f68`, אינו קיים ב־repo — כנראה ריצה על עותק אחר; כל הממצאים שנבדקו נכונים גם כאן).

**כל ממצאי ה־P0 אושרו,** למעט תיקון אחד: הסקירה כותבת ש־`score_p99` מחזיר 100 כשחסר P99. בפועל [parse_results_v2.py:360](scripts/parse_results_v2.py:360) מחזיר `0.0`. הפונקציות שכן מחזירות 100 על נתון חסר הן `score_stability` ([:374](scripts/parse_results_v2.py:374)) ו־`score_balance` ([:380](scripts/parse_results_v2.py:380)). הסיכון אמיתי, המנגנון שונה.

**הוכחה בשטח — הבעיה כבר השחיתה תוצאות קיימות.** `results/test4_10pods_50k_5050_32kb/` מכיל ריצה שבה כל עשרת הפודים נכשלו:

```
fio: ENOSPC on laying out file, stopping
fio: pid=0, err=28/file:filesetup.c:241, func=write, error=No space left on device
```

חצי ה־write של workload 50/50 מת לגמרי; `Run status` מדווח `READ` בלבד. הרצת ה־parser הקיים על התיקייה מחזירה:

```
OVERALL | 0.4 | 0.0 | 1.6 | 0.0 | ⚠️  WARN
```

ארבעה פודים ✅ PASS, שישה ⚠️ WARN, **אפס FAIL**, ואפס אזכור ל־ENOSPC. זו בדיוק שרשרת הכשל שהתוכנית מתקנת: PVC קטן מדי (Task 2) → fio נכשל חלקית → parser מתייחס ל־0 כמדידה תקפה (Task 8) → דוח ירוק.

**ממצאים נוספים שלא הופיעו בסקירה:**

1. **מסלולי `1pod`, `burst` ו־default ב־`deploy_test.sh` אינם מגדירים `pvc.size` כלל** — נשארת ברירת המחדל `10Gi` מ־[values.yaml](values.yaml), מול דרישה של 80–320 GiB. זהו הגורם הישיר ל־ENOSPC של test4. רק `gradual_scale`, `test17` ו־`test_example` מגדירים גודל.
2. **`.helmignore` אינו מחריג את `results/`** (2.6MB ומצטבר). כל `helm install` אורז את כל לוגי התוצאות לתוך ה־release secret, שמוגבל ל־~1MB דחוס. זו פצצת זמן.
3. **`to_ms()` בשני ה־parsers אינו מכיר `nsec`** — ו־`nsec` מופיע בפועל בלוגים הקיימים (`slat (nsec)` ב־[results/fio-test1.../fio-benchmark-0.log:38](results/fio-test1-10pods-30k-5050-4kb/fio-benchmark-0.log)). על התקנים מהירים גם `clat` יכול להיות `nsec`, ואז הערך יפורש כמילישניות — טעות של פי מיליון.
4. **`nififlow.py:515`** מחשב `p95_rate` כ־`_pct(rates,.95) * len(rows)` — percentile של קצב per-node כפול מספר ה־nodes. זו אינה p95 של הקלאסטר; היא מניחה שכל ה־nodes מגיעים ל־p95 שלהם באותו רגע.
5. **`cmd_flow` ב־`nifi-nfs-loadtest.sh:608`** בונה ומפעיל node אחר node בלולאה — node 0 כבר מייצר עומס בזמן ש־node 2 עדיין נבנה.

---

## Global Constraints

כל דרישה כאן חלה על כל Task בתוכנית:

- **Python: stdlib בלבד.** אין `pip install`. תואם Python 3.9+.
- **fio 3.41** — הגרסה ב־[Chart.yaml](Chart.yaml) `appVersion`. `--output-format=json+` נתמך.
- **כל שם משאב Kubernetes חייב להיות DNS-1123 label:** אותיות קטנות, ספרות ומקפים בלבד, עד 63 תווים. אין underscore.
- **כל ריצה מקבלת `RUN_ID`** בפורמט `<safe-test-id>-<YYYYmmdd-HHMMSS>`, מוטבע בכל Pod/PVC/ConfigMap כתווית `fio.benchmark/run-id`.
- **כל בחירה של משאבים לאיסוף או למחיקה חייבת להיות label-scoped ל־run-id.** אסור לבחור לפי `grep` על שם, ואסור למחוק לפי `app.kubernetes.io/name` לבדו.
- **`rate` ו־`rate_iops` הם per-clone ו־per-direction.** תקציב per-pod מתחלק ב־`numjobs`, ולעומס mixed נדרשים שני ערכים מופרדים בפסיק.
- **קיבולת נדרשת = `size` × `numjobs`.** כל PVC יוקצה עם headroom של 20% לפחות מעל הערך הזה.
- **ערך חסר הוא `None`, לא `0`.** אסור ל־parser להציג ציון או סטטוס על שדה חסר — הריצה נפסלת.
- **יחידות: MiB/s ו־GiB בלבד** בכל פלט. `fio` מדווח שניהם; אנחנו בוחרים binary ומתייגים מפורשות.

---

## תיקון מהותי — הסמנטיקה של `rate` ו־`rate_iops`

הסקירה קובעת ש"ערך יחיד מגביל read בלבד", וכל טבלת התיקונים שלה נשענת על זה. **הבדיקה מראה שזה לא נכון.** תיעוד fio אומר על `rate` ו־`rate_iops` שהם מקבלים ערכים "as described in blocksize", ושם כתוב במפורש: *"A single value applies to reads, writes, and trims."*

אומת אמפירית מול `fio-3.41` בתוך `rafmoshe2500/fio:3.41` — אותה גרסה ואותו image שהבדיקות רצות בו:

```
rate_iops=100   על randrw 50/50  ->  read: IOPS=99   write: IOPS=99
rate_iops=150,50 על randrw 70/30 ->  read: IOPS=149  write: IOPS=49
rate_iops=100   על randwrite     ->  write: IOPS=99
```

**הסמנטיקה הנכונה:** ערך יחיד מגביל **כל כיוון בנפרד** לאותו ערך. על job מעורב זה אומר שהתקציב האפקטיבי הוא **פי 2** מהערך שנכתב — לא "כתיבה ללא הגבלה".

מה זה משנה בפועל:

- החומרה נמוכה ממה שהסקירה מתארת. בדיקות עם scalar חורגות פי 2 מהיעד, לא באופן בלתי־חסום.
- על job חד־כיווני scalar הוא **תקין לחלוטין** — ולכן `test2` (שמונה sections חד־כיווניים), `test10_phase2` ו־`test11_phase2` נכונים כפי שהם.
- ההמלצות המספריות של הסקירה (`125,125`, `156,156`, `219,94`) נשארות נכונות למרות שהנימוק שגוי.

## נקודות החלטה — סגורות

**D1 — היקף היעד בשם הקובץ: per-pod.** *(אושר על ידי המשתמש, 2026-09-09.)*

שמות הבדיקות מציינים תקציב **לכל פוד**, לא סכום קלאסטר. `test1_10pods_30k_5050_4kb` פירושו 30,000 IOPS לפוד, כלומר 300,000 בקלאסטר של 10 פודים.

בשילוב עם תיקון הסמנטיקה למעלה, התמונה משתנה לטובה מאוד: **test2, test4, test5, test6, test10 (שני השלבים), test11 (שני השלבים) ו־test12 נכונים כבר עכשיו ואינם משתנים.** רק 12 קבצים דורשים תיקון rate, לא 20. הטבלה המחייבת ב־Task 4 מעודכנת בהתאם.

במיוחד: `test4` — הבדיקה שנכשלה בפועל — היה לה `rate_iops=3125,3125` נכון לחלוטין. הבאג היחיד שלה היה ה־PVC. היא לא רצה שגוי; היא רצה נכון ונחנקה.

**D2 — buffer policy: incompressible.** התוכנית מאחדת את כל הבדיקות על `refill_buffers=1 scramble_buffers=1` (נתונים בלתי־דחיסים, מדידה שמרנית מול array עם dedup/compression). זה יקר ב־CPU, ולכן Task 6 מעלה את המשאבים ומוסיף גילוי throttling. פרופיל `compressible` נוסף ב־P2 Task 19.

---

## מבנה קבצים

| קובץ | אחריות | Task |
|---|---|---|
| `scripts/lib/common.sh` | `SAFE_TEST_ID`, `RUN_ID`, עטיפת helm, label selectors | 1 |
| `templates/*.yaml` | `.Release.Namespace`, תוויות run-id, barrier, פלט JSON | 1, 3, 5, 7 |
| `values.yaml` | `runId`, `startEpoch`, `prepare`, `fioOutput` | 1, 5, 7 |
| `jobs/tests/*.fio` | תיקוני rate, bs, size | 2, 4 |
| `jobs/tests/*.meta.json` | יעד מוצהר לכל בדיקה — מקור האמת ל־parser | 4 |
| `scripts/fio_capacity.py` | חישוב קיבולת נדרשת מקובץ fio | 2 |
| `scripts/lib/fiojob.py` | פרסור `.fio` ל־JobSpec | 2 |
| `scripts/lib/fiojson.py` | פרסור `json+` עם ולידציה קשיחה | 8 |
| `scripts/lib/testmeta.py` | טעינת מטא־דאטה + אכיפת סכמה | 8 |
| `scripts/lib/report.py` | טבלאות, CSV, JSON export, cluster percentiles | 9 |
| `scripts/parse_results.py` | CLI יחיד (משוכתב) | 8, 9 |
| `scripts/tests/` | pytest-free unittest + fixtures | 2, 8, 9 |
| `scripts/deploy_test.sh` | פריסה עם barrier, preflight, run-id | 3, 5, 6, 7 |
| `scripts/collect_results.sh` | איסוף scoped + ולידציית שלמות | 10 |
| `scripts/cleanup_test.sh` | ניקוי scoped עם preview | 3 |
| `nifi/nififlow.py` | counters, failures, latency buckets, percentiles | 11–14 |
| `nifi/nfsstat.py` | פרסור `/proc/self/mountstats` | 15 |
| `nifi/nifi-nfs-loadtest.sh` | lifecycle מלא, anti-affinity, config hash | 12, 16 |
| `nifi/nifi-multi.sh` | barrier משותף, שמירה על apples-to-apples | 16 |

---

## סדר ביצוע

```
P0  ─ Tasks 1–10  ─ בלי זה אין להריץ שוב. מתקן שגיאות שמייצרות מספרים שקריים.
P1  ─ Tasks 11–17 ─ בלי זה אין להשוות בין ריצות או בין מערכי אחסון.
P2  ─ Tasks 18–22 ─ בלי זה אין לטעון שהבדיקה מייצגת NiFi production.
```

Tasks 1–3 חייבים להיות ראשונים ובסדר הזה — כל השאר תלוי ב־`RUN_ID` וב־namespace תקין. Tasks 4 ו־8 יכולים לרוץ במקביל אחריהם. Task 11 ואילך (NiFi) בלתי־תלויים לחלוטין ב־Tasks 1–10 ויכולים לרוץ במקביל בענף נפרד.

---

# P0 — לפני הריצה הבאה

## Task 1: זהות ריצה ו־namespace תקין

הבסיס לכל השאר. כרגע ה־release נרשם ב־namespace אחד וה־Pods נוצרים ב־`default`, ואין דרך לזהות אילו משאבים שייכים לאיזו ריצה.

**Files:**
- Create: `scripts/lib/common.sh`
- Modify: `templates/configmap.yaml`, `templates/pods.yaml:7`, `templates/pvc.yaml:9`, `templates/_helpers.tpl`
- Modify: `values.yaml`

**Interfaces:**
- Produces: `SAFE_TEST_ID`, `RUN_ID`, `run_selector()`, `helm_deploy()` — נצרכים ב־Tasks 3, 5, 6, 7, 10
- Produces: תווית `fio.benchmark/run-id` על כל Pod/PVC/ConfigMap

- [ ] **Step 1: כתוב את ספריית ה־shell המשותפת**

`scripts/lib/common.sh`:

```bash
#!/bin/bash
# Shared helpers for the fio benchmark scripts. Source this, do not execute it.
#
# Every Kubernetes name we generate has to be a DNS-1123 label: lowercase
# alphanumerics and hyphens only. The test IDs use underscores, so a single
# sanitised variable is derived once and used for every name.

CHART_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." >/dev/null 2>&1 && pwd)"

RUN_LABEL_KEY="fio.benchmark/run-id"
TEST_LABEL_KEY="fio.benchmark/test-id"

# safe_id <test_id> -> DNS-1123 safe form
safe_id() {
  local s="${1//_/-}"
  s="$(printf '%s' "$s" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9-')"
  s="${s#-}"; s="${s%-}"
  printf '%s' "${s:0:40}"
}

# new_run_id <safe_test_id> -> "<safe>-<timestamp>", <= 63 chars
new_run_id() {
  printf '%s-%s' "$1" "$(date +%Y%m%d-%H%M%S)"
}

# run_selector <run_id> -> label selector string for kubectl
run_selector() {
  printf '%s=%s' "$RUN_LABEL_KEY" "$1"
}

# helm_deploy <release> <namespace> <run_id> [extra helm args...]
# Always upgrade --install so a re-run is idempotent, always --wait so the
# caller knows the pods exist before it does anything that assumes they do,
# always --atomic so a failed deploy does not leave half a test running.
helm_deploy() {
  local release="$1" ns="$2" run_id="$3"; shift 3
  helm upgrade --install "$release" "$CHART_DIR" \
    -n "$ns" --create-namespace \
    --wait --atomic --timeout 15m \
    --set runId="$run_id" \
    "$@"
}

die() { printf '\033[1;31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }
log() { printf '\033[1;34m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
```

- [ ] **Step 2: בדוק שהסניטציה נכונה**

```bash
bash -c 'source scripts/lib/common.sh; safe_id test1_10pods_30k_5050_4kb; echo; safe_id "Test_7__1POD"; echo'
```

Expected:
```
test1-10pods-30k-5050-4kb
test-7--1pod
```

- [ ] **Step 3: הוסף את הערכים החדשים ל־values.yaml**

הוסף בסוף [values.yaml](values.yaml):

```yaml
# Unique identifier for this benchmark run. Set by scripts/deploy_test.sh.
# Every Pod, PVC and ConfigMap carries it as a label so collection and
# cleanup can select exactly this run and nothing else.
runId: ""

# Absolute epoch seconds at which every pod should start fio. Lets several
# releases begin at the same instant regardless of when helm returned.
# Empty means "start immediately".
startEpoch: ""
```

- [ ] **Step 4: הוסף helper לתוויות הריצה**

הוסף בסוף [templates/_helpers.tpl](templates/_helpers.tpl):

```
{{/*
Run identity labels. Applied to every object so collection and cleanup can
select one run precisely instead of guessing from name prefixes.
*/}}
{{- define "fio-benchmark.runLabels" -}}
{{- if .Values.runId }}
fio.benchmark/run-id: {{ .Values.runId | quote }}
{{- end }}
{{- end }}
```

- [ ] **Step 5: החלף `.Values.namespace` ב־`.Release.Namespace` ותוסיף את התוויות**

ב־[templates/configmap.yaml](templates/configmap.yaml) החלף שורה 6 ותוסיף תוויות:

```yaml
  namespace: {{ .Release.Namespace }}
  labels:
    {{- include "fio-benchmark.labels" . | nindent 4 }}
    {{- include "fio-benchmark.runLabels" . | nindent 4 }}
```

ב־[templates/pods.yaml:7](templates/pods.yaml) ו־[templates/pvc.yaml:9](templates/pvc.yaml) בצע את אותו שינוי — `namespace: {{ $.Release.Namespace }}` ו־`{{- include "fio-benchmark.runLabels" $ | nindent 4 }}` אחרי בלוק ה־labels הקיים.

- [ ] **Step 6: מחק את `namespace` מ־values.yaml**

הסר את השורות מ־[values.yaml](values.yaml):

```yaml
# OpenShift/Kubernetes namespace
namespace: default
```

זהו התיקון האמיתי: מקור אמת יחיד ל־namespace הוא `helm -n`. השארת `.Values.namespace` תשחזר את הבאג בעוד חצי שנה.

- [ ] **Step 7: אמת שה־rendering נכון**

```bash
helm template t . -n fio-tests --set runId=demo-20260909-120000 --set replicaCount=1 | grep -E "namespace:|run-id:"
```

Expected: כל `namespace:` הוא `fio-tests`, וכל אובייקט נושא `fio.benchmark/run-id: "demo-20260909-120000"`. אפס מופעים של `default`.

- [ ] **Step 8: החרג את results/ מהחבילה**

הוסף ל־[.helmignore](.helmignore):

```
results/
scripts/
nifi/
docs/
```

- [ ] **Step 9: אמת שהחבילה התכווצה**

```bash
helm package . -d /tmp/chartcheck && ls -la /tmp/chartcheck/
```

Expected: קובץ `.tgz` בסדר גודל של עשרות KB, לא מגה־בייטים.

- [ ] **Step 10: Commit**

```bash
git add scripts/lib/common.sh templates/ values.yaml .helmignore && git commit -m "fix: single source of truth for namespace, add run-id identity to all resources"
```

---

## Task 2: preflight קיבולת — הבאג שהרג את test4

`size` הוא per-clone. `size=10G` עם `numjobs=8` דורש 80 GiB, וברירת המחדל היא PVC של 10Gi. Task זה בונה את הכלי שמסרב להתחיל במקום להיכשל אחרי עשר דקות.

**Files:**
- Create: `scripts/lib/__init__.py`, `scripts/lib/fiojob.py`, `scripts/fio_capacity.py`
- Create: `scripts/tests/__init__.py`, `scripts/tests/test_fiojob.py`

**Interfaces:**
- Produces: `parse_job_file(path) -> JobSpec` עם `.required_bytes`, `.numjobs`, `.directions` — נצרך ב־Tasks 3, 8

- [ ] **Step 1: כתוב את הטסט הכושל**

`scripts/tests/test_fiojob.py`:

```python
import os
import tempfile
import unittest

from lib.fiojob import parse_job_file, parse_size


class ParseSizeTest(unittest.TestCase):
    def test_binary_suffixes(self):
        self.assertEqual(parse_size("10G"), 10 * 1024 ** 3)
        self.assertEqual(parse_size("512k"), 512 * 1024)
        self.assertEqual(parse_size("1m"), 1024 ** 2)
        self.assertEqual(parse_size("4096"), 4096)


class ParseJobFileTest(unittest.TestCase):
    def _write(self, text):
        fd, path = tempfile.mkstemp(suffix=".fio")
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        self.addCleanup(os.unlink, path)
        return path

    def test_global_numjobs_multiplies_size(self):
        path = self._write(
            "[global]\nsize=10G\nnumjobs=8\nrw=randrw\nrwmixread=50\nbs=4k\n\n"
            "[job1]\nrate_iops=188,187\n"
        )
        spec = parse_job_file(path)
        self.assertEqual(spec.numjobs, 8)
        self.assertEqual(spec.required_bytes, 8 * 10 * 1024 ** 3)
        self.assertEqual(spec.directions, {"read", "write"})

    def test_eight_sections_without_numjobs_sum_to_eight_clones(self):
        body = "[global]\nsize=10G\n\n"
        for i in range(4):
            body += f"[w{i}]\nrw=randwrite\nbs=32k\nrate_iops=375\n\n"
            body += f"[r{i}]\nrw=randread\nbs=32k\nrate_iops=375\n\n"
        spec = parse_job_file(self._write(body))
        self.assertEqual(spec.numjobs, 8)
        self.assertEqual(spec.required_bytes, 8 * 10 * 1024 ** 3)

    def test_write_only_direction(self):
        spec = parse_job_file(
            self._write("[global]\nsize=1G\nnumjobs=2\n\n[j]\nrw=randwrite\nbs=4k\n")
        )
        self.assertEqual(spec.directions, {"write"})

    def test_inline_comment_is_stripped(self):
        spec = parse_job_file(
            self._write("[global]\nsize=10G   ; per job\nnumjobs=8\n\n[j]\nrw=read\n")
        )
        self.assertEqual(spec.required_bytes, 8 * 10 * 1024 ** 3)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: הרץ את הטסט וודא שהוא נכשל**

```bash
cd scripts && python3 -m unittest tests.test_fiojob -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'lib.fiojob'`

- [ ] **Step 3: כתוב את המימוש המינימלי**

`scripts/lib/__init__.py` — קובץ ריק.

`scripts/lib/fiojob.py`:

```python
"""Parse a fio job file well enough to know what it will demand of the
storage before we ask a cluster for it.

The one thing this module exists to get right: `size` is per clone, and the
number of clones is `numjobs` from [global] multiplied across sections, or
the section count when numjobs is absent. Getting that wrong is what makes
a 10Gi PVC accept a job that needs 80GiB and die with ENOSPC ten minutes in.
"""

import re
from dataclasses import dataclass, field

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


@dataclass
class JobSpec:
    path: str
    numjobs: int = 1
    size_bytes: int = 0
    directions: set = field(default_factory=set)
    sections: list = field(default_factory=list)
    globals: dict = field(default_factory=dict)

    @property
    def required_bytes(self):
        return self.size_bytes * self.numjobs

    def required_gib(self, headroom=1.2):
        return self.required_bytes * headroom / 1024 ** 3


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
    real job files use (see test7's annotated [global] block)."""
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
    spec.size_bytes = parse_size(glb.get("size", "0"))

    # numjobs from [global] applies to every section. Without it each
    # section is one clone, so eight sections are eight clones.
    if "numjobs" in glb:
        spec.numjobs = int(glb["numjobs"]) * max(len(jobs), 1)
    else:
        spec.numjobs = sum(int(s.get("numjobs", 1)) for s in jobs) or 1

    for s in jobs:
        spec.directions |= _directions_for(s.get("rw", glb.get("rw", "")))
    if not spec.directions:
        spec.directions = _directions_for(glb.get("rw", ""))

    return spec
```

- [ ] **Step 4: הרץ את הטסטים וודא שהם עוברים**

```bash
cd scripts && python3 -m unittest tests.test_fiojob -v
```

Expected: `Ran 5 tests ... OK`

- [ ] **Step 5: כתוב את ה־CLI**

`scripts/fio_capacity.py`:

```python
#!/usr/bin/env python3
"""Print the PVC size a fio job file actually needs.

    ./fio_capacity.py jobs/tests/test4_10pods_50k_5050_32kb.fio
    96

Exit 1 and explain if --pvc is given and is too small. This is the check
that would have stopped the test4 ENOSPC run before it started.
"""

import argparse
import math
import sys

from lib.fiojob import parse_job_file, parse_size


def main():
    p = argparse.ArgumentParser()
    p.add_argument("job_file")
    p.add_argument("--headroom", type=float, default=1.2,
                   help="multiplier over size*numjobs (default 1.2)")
    p.add_argument("--pvc", default="",
                   help="proposed PVC size, e.g. 96Gi; exits 1 if too small")
    a = p.parse_args()

    spec = parse_job_file(a.job_file)
    if spec.size_bytes == 0:
        sys.exit(f"{a.job_file}: no size= in [global]; cannot size a PVC")

    need_gib = spec.required_gib(a.headroom)
    rounded = int(math.ceil(need_gib))

    if not a.pvc:
        print(rounded)
        return

    have = parse_size(a.pvc.rstrip("i")) if a.pvc.lower().endswith("gi") \
        else parse_size(a.pvc)
    if have < spec.required_bytes * a.headroom:
        sys.exit(
            f"{a.job_file}: PVC {a.pvc} is too small.\n"
            f"  size={spec.size_bytes / 1024 ** 3:.0f}GiB x numjobs={spec.numjobs}"
            f" = {spec.required_bytes / 1024 ** 3:.0f}GiB of dataset\n"
            f"  with {int((a.headroom - 1) * 100)}% headroom that is {rounded}Gi\n"
            f"  fio will fail with ENOSPC partway through the run."
        )
    print(rounded)


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: הרץ אותו על הבדיקה שנכשלה בפועל**

```bash
cd scripts && python3 fio_capacity.py ../jobs/tests/test4_10pods_50k_5050_32kb.fio --pvc 10Gi
```

Expected: exit 1 עם ההודעה `size=10GiB x numjobs=8 = 80GiB of dataset` — בדיוק הכשל שהתרחש.

- [ ] **Step 7: הפק את טבלת הקיבולת לכל הבדיקות**

```bash
cd scripts && for f in ../jobs/tests/*.fio; do printf '%-45s %sGi\n' "$(basename $f)" "$(python3 fio_capacity.py $f)"; done
```

Expected: הפלט תואם את הטבלה ב־Task 3 Step 2. אם לא — הטבלה שגויה, לא הכלי.

- [ ] **Step 8: Commit**

```bash
git add scripts/lib scripts/tests scripts/fio_capacity.py && git commit -m "feat: fio capacity preflight - size is per clone, numjobs multiplies it"
```

---

## Task 3: `deploy_test.sh` — נתיבים, שמות, PVC ו־preflight

זה ה־Task שהופך את הפריסה מ"נכשלת בשקט" ל"מסרבת להתחיל". שלוש בדיקות `1pod` מפנות לקובץ שאינו קיים, כל השמות מכילים underscore, ורוב המסלולים לא מגדירים PVC.

**Files:**
- Modify: `scripts/deploy_test.sh` (שכתוב מלא)
- Modify: `scripts/cleanup_test.sh`

**Interfaces:**
- Consumes: `safe_id()`, `new_run_id()`, `helm_deploy()`, `run_selector()` מ־Task 1; `fio_capacity.py` מ־Task 2
- Produces: `results/<run_id>/manifest.json` — נצרך ב־Task 10

- [ ] **Step 1: הוסף טבלת קיבולת מפורשת**

צור `scripts/pvc_sizes.conf` — מקור האמת לגודל PVC לכל בדיקה, מחושב מ־Task 2 Step 7:

```
# test_id                          pvc_size
test1_10pods_30k_5050_4kb          96Gi
test2_10pods_30k_5050_32kb         96Gi
test3_10pods_50k_5050_4kb          96Gi
test4_10pods_50k_5050_32kb         96Gi
test5_10pods_3gb_7030_256kb        192Gi
test6_10pods_3gb_7030_512kb        384Gi
test7_1pod_max_write_4kb           96Gi
test8_1pod_max_read_4kb            96Gi
test9_1pod_max_rw_7030_4kb         96Gi
test10_burst_write_phase1          192Gi
test10_burst_write_phase2          384Gi
test11_burst_read_phase1           192Gi
test11_burst_read_phase2           384Gi
test12_gradual_scale_32kb_7030     96Gi
test13_gradual_scale_512kb_7030    192Gi
test14_gradual_scale_1mb_7030      250Gi
test15_gradual_scale_512kb_5050    192Gi
test16_gradual_scale_1mb_5050      250Gi
test17_mixed_workload_32k          96Gi
test17_mixed_workload_64k          96Gi
test17_mixed_workload_256k         192Gi
test17_mixed_workload_512k         192Gi
test_example_phase1                4Gi
test_example_phase2                4Gi
```

- [ ] **Step 2: כתוב את הפונקציות המשותפות החדשות**

הוסף ל־`scripts/lib/common.sh`:

```bash
# pvc_size_for <test_id> -> size string, dies if unknown
pvc_size_for() {
  local id="$1" conf="$CHART_DIR/scripts/pvc_sizes.conf" name size
  while read -r name size _rest; do
    [[ -z "${name:-}" || "${name:0:1}" == "#" ]] && continue
    [[ "$name" == "$id" ]] && { printf '%s' "$size"; return 0; }
  done < "$conf"
  die "no PVC size registered for '$id' in scripts/pvc_sizes.conf.
  Compute it with: python3 scripts/fio_capacity.py jobs/tests/${id}.fio"
}

# job_file_for <test_id> -> absolute path, dies if missing
job_file_for() {
  local f="$CHART_DIR/jobs/tests/$1.fio"
  [[ -f "$f" ]] || die "no such fio job file: $f"
  printf '%s' "$f"
}

# preflight <test_id> — refuses to deploy a job that cannot fit its PVC
preflight() {
  local id="$1" job pvc
  job="$(job_file_for "$id")"
  pvc="$(pvc_size_for "$id")"
  ( cd "$CHART_DIR/scripts" && python3 fio_capacity.py "$job" --pvc "$pvc" >/dev/null ) \
    || die "capacity preflight failed for $id"
  log "preflight ok: $id needs <= $pvc"
}
```

- [ ] **Step 3: שכתב את `deploy_test.sh`**

`scripts/deploy_test.sh`:

```bash
#!/bin/bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

TEST_ID="${1:-}"
NAMESPACE="${2:-fio-tests}"

if [ -z "$TEST_ID" ]; then
  echo "Usage: ./deploy_test.sh <test_id> [namespace]"
  echo "Example: ./deploy_test.sh test1_10pods_30k_5050_4kb fio-tests"
  echo
  echo "Available tests:"
  for f in "$CHART_DIR"/jobs/tests/*.fio; do echo "  $(basename "$f" .fio)"; done
  exit 1
fi

SAFE_TEST_ID="$(safe_id "$TEST_ID")"
RUN_ID="$(new_run_id "$SAFE_TEST_ID")"
STORAGE_CLASS="${STORAGE_CLASS:-sc-nas-nfs3}"

# Every pod waits for this absolute instant before starting fio, so all
# releases in a multi-release test begin together no matter when helm
# returned. 180s covers PVC binding and image pull on a cold node.
BARRIER_LEAD="${BARRIER_LEAD:-180}"
START_EPOCH=$(( $(date +%s) + BARRIER_LEAD ))

RESULTS_DIR="$CHART_DIR/results/$RUN_ID"
mkdir -p "$RESULTS_DIR"

log "test=$TEST_ID run=$RUN_ID ns=$NAMESPACE sc=$STORAGE_CLASS"
log "synchronised start at $(date -d "@$START_EPOCH" 2>/dev/null || date -r "$START_EPOCH")"

# deploy_release <release-suffix> <replicas> <test_id_for_job_file> [extra...]
deploy_release() {
  local suffix="$1" replicas="$2" job_id="$3"; shift 3
  preflight "$job_id"
  local release="fio-${SAFE_TEST_ID}${suffix:+-$suffix}"
  release="${release:0:53}"
  log "deploying $release ($replicas pods, job $job_id)"
  helm_deploy "$release" "$NAMESPACE" "$RUN_ID" \
    --set replicaCount="$replicas" \
    --set namePrefix="fio-${SAFE_TEST_ID}${suffix:+-$suffix}" \
    --set startEpoch="$START_EPOCH" \
    --set pvc.storageClassName="$STORAGE_CLASS" \
    --set pvc.size="$(pvc_size_for "$job_id")" \
    --set-file fioJob.content="$(job_file_for "$job_id")" \
    "$@"
  RELEASES+=("$release")
}

RELEASES=()

case $TEST_ID in
  *1pod*)
    deploy_release "" 1 "$TEST_ID"
    ;;

  *test10_burst_write*)
    # Both phases are deployed up front so PVCs are bound and images pulled
    # before either starts. The barrier, not a sleep, staggers them.
    deploy_release "p1" 5 test10_burst_write_phase1
    deploy_release "p2" 5 test10_burst_write_phase2 \
      --set startEpoch=$((START_EPOCH + 300))
    ;;

  *test11_burst_read*)
    deploy_release "p1" 20 test11_burst_read_phase1
    deploy_release "p2" 5 test11_burst_read_phase2 \
      --set startEpoch=$((START_EPOCH + 300))
    ;;

  *test_example*)
    deploy_release "p1" 10 test_example_phase1 \
      --set resources.requests.cpu=100m --set resources.requests.memory=128Mi
    deploy_release "p2" 1 test_example_phase2 \
      --set startEpoch=$((START_EPOCH + 120)) \
      --set resources.requests.cpu=100m --set resources.requests.memory=128Mi
    ;;

  *gradual_scale*)
    # See Task 7: stepped scaling replaces the old per-minute upgrade loop.
    "$CHART_DIR/scripts/deploy_scale_steps.sh" "$TEST_ID" "$NAMESPACE" "$RUN_ID"
    exit $?
    ;;

  *test17_mixed_workload*)
    deploy_release "32k"  13 test17_mixed_workload_32k
    deploy_release "64k"  17 test17_mixed_workload_64k
    deploy_release "256k"  7 test17_mixed_workload_256k
    deploy_release "512k"  3 test17_mixed_workload_512k
    ;;

  *)
    deploy_release "" 10 "$TEST_ID"
    ;;
esac

# The manifest is what makes a run reproducible and what collect_results.sh
# validates against. Without it a set of logs is just a set of logs.
cat > "$RESULTS_DIR/manifest.json" <<JSON
{
  "run_id": "$RUN_ID",
  "test_id": "$TEST_ID",
  "namespace": "$NAMESPACE",
  "storage_class": "$STORAGE_CLASS",
  "releases": [$(printf '"%s",' "${RELEASES[@]}" | sed 's/,$//')],
  "start_epoch": $START_EPOCH,
  "git_commit": "$(git -C "$CHART_DIR" rev-parse HEAD)",
  "git_dirty": $(if [ -n "$(git -C "$CHART_DIR" status --porcelain)" ]; then echo true; else echo false; fi),
  "fio_image": "$(helm get values "${RELEASES[0]}" -n "$NAMESPACE" -a -o json | python3 -c 'import json,sys; v=json.load(sys.stdin); print(v["image"]["repository"]+":"+str(v["image"]["tag"]))')",
  "deployed_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
JSON

# Machine-readable handoff. Callers must not have to scrape the log for the
# run id -- run_repeated.sh depends on this file.
printf '%s\n' "$RUN_ID" > "$CHART_DIR/results/.last_run_id"

log "manifest: $RESULTS_DIR/manifest.json"
log "collect with: ./scripts/collect_results.sh $RUN_ID $NAMESPACE"
```

- [ ] **Step 4: תקן את `cleanup_test.sh`**

`scripts/cleanup_test.sh`:

```bash
#!/bin/bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

RUN_ID="${1:-}"
NAMESPACE="${2:-fio-tests}"
FORCE="${FORCE:-false}"

if [ -z "$RUN_ID" ]; then
  echo "Usage: ./cleanup_test.sh <run_id> [namespace]"
  echo "Run IDs are printed by deploy_test.sh and stored in results/<run_id>/manifest.json"
  echo
  echo "Known runs in $NAMESPACE:"
  kubectl get pods -n "$NAMESPACE" -o jsonpath="{range .items[*]}{.metadata.labels['fio\.benchmark/run-id']}{'\n'}{end}" 2>/dev/null | sort -u | sed 's/^/  /'
  exit 1
fi

SEL="$(run_selector "$RUN_ID")"

echo "The following resources match ${SEL} and WILL BE DELETED:"
kubectl get pods,pvc,configmap -n "$NAMESPACE" -l "$SEL" 2>/dev/null || true
echo

if [ "$FORCE" != "true" ]; then
  read -rp "Proceed? [y/N] " a
  [[ "$a" == "y" ]] || { log "aborted"; exit 0; }
fi

# Uninstall by release, recorded in the manifest rather than guessed.
MANIFEST="$CHART_DIR/results/$RUN_ID/manifest.json"
if [ -f "$MANIFEST" ]; then
  while read -r release; do
    log "uninstalling $release"
    helm uninstall "$release" -n "$NAMESPACE" || warn "$release already gone"
  done < <(python3 -c 'import json,sys; [print(r) for r in json.load(open(sys.argv[1]))["releases"]]' "$MANIFEST")
else
  warn "no manifest for $RUN_ID; falling back to label-scoped deletion only"
fi

# Scoped to this run. Never delete by app.kubernetes.io/name alone -- that
# takes out concurrent runs and prepared read datasets with it.
kubectl delete pvc -n "$NAMESPACE" -l "$SEL" --wait=false || true

log "cleanup completed for $RUN_ID"
```

- [ ] **Step 5: אמת תחביר**

```bash
bash -n scripts/deploy_test.sh && bash -n scripts/cleanup_test.sh && bash -n scripts/lib/common.sh && echo "syntax ok"
```

Expected: `syntax ok`

- [ ] **Step 6: אמת ש־preflight חוסם**

```bash
bash -c 'source scripts/lib/common.sh; job_file_for test7_1pod_max_write_4kb'
```

Expected: הנתיב `.../jobs/tests/test7_1pod_max_write_4kb.fio` — הקובץ שקיים באמת, במקום `fio-test7-1pod-max-write-4kb.fio` שהקוד הישן חיפש.

- [ ] **Step 7: Commit**

```bash
git add scripts/ && git commit -m "fix: correct 1pod job paths, DNS-safe names, per-test PVC sizing, run-scoped cleanup"
```

---

## Task 4: תיקון ערכי rate ומטא־דאטה לכל בדיקה

`rate_iops` הוא per-clone ו־per-direction, וערך יחיד מגביל **כל כיוון בנפרד** לאותו ערך (אומת אמפירית — ראה "תיקון מהותי" למעלה). לכן job מעורב עם scalar חורג פי 2 מהיעד, ו־job חד־כיווני עם scalar תקין.

בשילוב עם D1 (השם הוא per-pod), הנוסחה לכל בדיקה מעורבת היא:

```
per_clone_total = target_iops_per_pod / numjobs
read_cap        = per_clone_total * rwmixread
write_cap       = per_clone_total * (1 - rwmixread)
rate_iops = <read_cap>,<write_cap>
```

**Files:**
- Modify: כל `jobs/tests/*.fio` (23 קבצים)
- Create: `jobs/tests/*.meta.json` (23 קבצים)

**Interfaces:**
- Produces: `jobs/tests/<test_id>.meta.json` — המקור היחיד ליעדים; נצרך ב־Task 8 ו־Task 9

- [ ] **Step 1: החל את הטבלה המחייבת**

לכל קובץ, החלף את שורת ה־rate ואת ההערה שמעליה:

| קובץ | per-pod target | numjobs | ערך נוכחי | בפועל נותן | שורה חדשה |
|---|---|---|---|---|---|
| `test1_10pods_30k_5050_4kb` | 30,000 IOPS | 8 | `3750` | 60,000 (×2) | **`rate_iops=1875,1875`** |
| `test2_10pods_30k_5050_32kb` | 30,000 IOPS | 8 sections | `3750` ×8 | 30,000 ✓ | ללא שינוי |
| `test3_10pods_50k_5050_4kb` | 50,000 IOPS | 8 | `625` | 10,000 (×0.2) | **`rate_iops=3125,3125`** |
| `test4_10pods_50k_5050_32kb` | 50,000 IOPS | 8 | `3125,3125` | 50,000 ✓ | ללא שינוי |
| `test5_10pods_3gb_7030_256kb` | 3 GiB/s | 8 | `262m,113m` | 3 GiB/s ✓ | ללא שינוי |
| `test6_10pods_3gb_7030_512kb` | 3 GiB/s | 8 | `262m,113m` | 3 GiB/s ✓ | ללא שינוי |
| `test10_burst_write_phase1` | 3 GiB/s | 8 | `262m,113m` | 3 GiB/s ✓ | ללא שינוי |
| `test10_burst_write_phase2` | 10 GiB/s | 8 | `1250m` | 10 GiB/s ✓ | ללא שינוי (חד־כיווני) |
| `test11_burst_read_phase1` | 5 GiB/s | 8 | `437m,188m` | 5 GiB/s ✓ | ללא שינוי |
| `test11_burst_read_phase2` | 10 GiB/s | 8 | `1250m` | 10 GiB/s ✓ | ללא שינוי (חד־כיווני) |
| `test12_gradual_scale_32kb_7030` | 10,000 IOPS | 8 | `875,375` | 10,000 ✓ | ללא שינוי |
| `test13_gradual_scale_512kb_7030` | 2,500 IOPS | 8 | `70,30` | 800 (×0.32) | **`rate_iops=219,94`** + `bs=128k`→`bs=512k` |
| `test14_gradual_scale_1mb_7030` | 2,500 IOPS | 8 | `312` | 4,992 (×2) | **`rate_iops=219,94`** + `size=40G`→`25G` |
| `test15_gradual_scale_512kb_5050` | 2,500 IOPS | 8 | `312` | 4,992 (×2) | **`rate_iops=156,156`** |
| `test16_gradual_scale_1mb_5050` | 2,500 IOPS | 8 | `312` | 4,992 (×2) | **`rate_iops=156,156`** + `size=40G`→`25G` |
| `test17_mixed_workload_32k` | 2,000 IOPS | 8 | `250` | 4,000 (×2) | **`rate_iops=125,125`** |
| `test17_mixed_workload_64k` | 2,000 IOPS | 8 | `250` | 4,000 (×2) | **`rate_iops=125,125`** |
| `test17_mixed_workload_256k` | 2,000 IOPS | 8 | `250` | 4,000 (×2) | **`rate_iops=125,125`** |
| `test17_mixed_workload_512k` | 2,000 IOPS | 8 | `250` | 4,000 (×2) | **`rate_iops=125,125`** |
| `test_example_phase1` | 100 IOPS | 2 | `350,150` | 1,000 (×10) | **`rate_iops=35,15`** |
| `test_example_phase2` | 500 IOPS | 2 | `700,700` | 2,800 (×5.6), יחס 50/50 | **`rate_iops=175,75`** |
| `test7/8/9_1pod_max_*` | uncapped | 8 | אין `rate` | תקרה | ללא שינוי — ראה Step 3 |

**עשר בדיקות כבר נכונות ואין לגעת בהן.** שתים־עשרה דורשות תיקון, מהן שמונה הן חריגה של פי 2 בדיוק (ה־scalar על job מעורב), אחת חריגה פי 10, אחת פי 5.6, ואחת (`test3`) דווקא **נמוכה** פי 5 מהיעד — כלומר מדדה עומס קל בהרבה ממה שהשם מבטיח.

`test14`/`test16`: `size=25G × 8 = 200GiB` נכנס ל־PVC של 250Gi עם 20% headroom. עם `size=40G` הדרישה היא 320GiB — מעל ה־PVC, וזו אותה שרשרת ENOSPC של test4.

- [ ] **Step 2: אחד את בלוק ה־[global]**

בכל 23 הקבצים, ודא שבלוק `[global]` מכיל את השורות הבאות. בדיקות שאין להן ramp כרגע (test1, test3, test5, test6, test8, test10–test17, test_example) מקבלות אותו — בלעדיו אי אפשר להשוות ביניהן:

```ini
ramp_time=60
randrepeat=0
norandommap=1
refill_buffers=1
scramble_buffers=1
randseed=20260909

# Time series. Without these there is no way to tell a steady 10K IOPS from
# 20K for five minutes and zero for five more.
write_iops_log=/tmp/fiolog
write_bw_log=/tmp/fiolog
write_lat_log=/tmp/fiolog
log_avg_msec=1000
per_job_logs=1
```

- [ ] **Step 3: הסר `cpus_allowed_policy=split`**

מ־`test2`, `test7`, `test9`. עם CPU limit של 2–8 ליבות ו־8 jobs, `split` מקצה לכל job ליבה נפרדת מתוך ה־cpuset של ה־container. ב־cgroup עם quota זה לא מבודד כלום, ואם יש פחות ליבות מ־jobs — fio נכשל. אין להשתמש בו בלי CPU manager static policy.

- [ ] **Step 4: הוסף שלב prepare ל־test8 ו־test11-phase2**

שתיהן בדיקות read על PVC חדש. בלי dataset מוכן הן קוראות קבצים sparse או ריקים ומודדות את המהירות של אוויר. הוסף ל־`[global]` בשני הקבצים:

```ini
# The dataset is laid out by the prepare initContainer (see Task 5).
# Refuse to silently create a sparse file if that did not happen.
allow_file_create=0
```

- [ ] **Step 5: כתוב קובץ מטא־דאטה לכל בדיקה**

`jobs/tests/test1_10pods_30k_5050_4kb.meta.json`:

```json
{
  "test_id": "test1_10pods_30k_5050_4kb",
  "replicas": 10,
  "numjobs": 8,
  "block_size": "4k",
  "pattern": "randrw",
  "rw_mix_read": 0.5,
  "aggregate_scope": "cluster",
  "target_iops_total": 30000,
  "target_iops_per_pod": 3000,
  "target_bw_mibps_total": null,
  "expected_directions": ["read", "write"],
  "runtime_s": 600,
  "ramp_s": 60,
  "pvc_size_gib": 96,
  "dataset_gib_per_pod": 80,
  "rate_limited": true
}
```

`jobs/tests/test5_10pods_3gb_7030_256kb.meta.json` — שים לב ל־`aggregate_scope: "per_pod"` שמקודד את החלטה D1 מפורשות:

```json
{
  "test_id": "test5_10pods_3gb_7030_256kb",
  "replicas": 10,
  "numjobs": 8,
  "block_size": "256k",
  "pattern": "randrw",
  "rw_mix_read": 0.7,
  "aggregate_scope": "per_pod",
  "target_iops_total": null,
  "target_iops_per_pod": null,
  "target_bw_mibps_per_pod": 3000,
  "target_bw_mibps_total": 30000,
  "expected_directions": ["read", "write"],
  "runtime_s": 600,
  "ramp_s": 60,
  "pvc_size_gib": 192,
  "dataset_gib_per_pod": 160,
  "rate_limited": true
}
```

`jobs/tests/test7_1pod_max_write_4kb.meta.json` — בדיקת תקרה, ללא יעד:

```json
{
  "test_id": "test7_1pod_max_write_4kb",
  "replicas": 1,
  "numjobs": 8,
  "block_size": "4k",
  "pattern": "randwrite",
  "rw_mix_read": 0.0,
  "aggregate_scope": "per_pod",
  "target_iops_total": null,
  "target_iops_per_pod": null,
  "target_bw_mibps_total": null,
  "expected_directions": ["write"],
  "runtime_s": 600,
  "ramp_s": 60,
  "pvc_size_gib": 96,
  "dataset_gib_per_pod": 80,
  "rate_limited": false,
  "total_queue_depth": 1024,
  "note": "Ceiling test: iodepth=128 x numjobs=8 = 1024 outstanding IOs. Latency figures from this run describe a saturated queue and must not be compared with the rate-limited tests."
}
```

כתוב קובץ מקביל לכל 23 הבדיקות באותה סכמה. `expected_directions` ו־`rate_limited` הם השדות ש־Task 8 משתמש בהם כדי לפסול ריצה — קובץ שמצהיר `["read","write"]` וריצה שהחזירה write בלבד נפסלת, וזה בדיוק מה ש־test4 היה צריך לעשות.

- [ ] **Step 6: אמת שכל קובץ fio תקין ותואם למטא־דאטה**

```bash
cd scripts && for f in ../jobs/tests/*.fio; do id=$(basename "$f" .fio); test -f "../jobs/tests/$id.meta.json" || echo "MISSING META: $id"; done; echo "meta check done"
```

Expected: `meta check done` ללא שורות MISSING.

- [ ] **Step 7: אמת ש־fio מקבל את הקבצים**

```bash
for f in jobs/tests/*.fio; do fio --parse-only "$f" >/dev/null 2>&1 || echo "INVALID: $f"; done; echo "fio parse done"
```

Expected: `fio parse done` ללא שורות INVALID. (אם fio אינו מותקן מקומית, הרץ בתוך image הבדיקה.)

- [ ] **Step 8: Commit**

```bash
git add jobs/tests/ && git commit -m "fix: rate is per-clone per-direction; correct bs/size; add declared targets per test"
```

---

## Task 5: barrier מסונכרן ו־prepare במקום sleep

`helm install` חוזר לפני שכל PVC נקשר, כל image נמשך וכל fio התחיל. `sleep 300` מודד זמן מהחזרת helm, לא מתחילת phase1. החפיפה שהבדיקות מניחות אינה מובטחת.

**Files:**
- Modify: `templates/pods.yaml`
- Modify: `values.yaml`

**Interfaces:**
- Consumes: `.Values.startEpoch` מ־Task 1
- Produces: פלט `===FIO_JSON_BEGIN===` / `===FIO_TSLOG_BEGIN===` בלוג — נצרך ב־Task 10 ו־Task 8

- [ ] **Step 1: הוסף את ערכי ה־prepare ל־values.yaml**

```yaml
# Lay the dataset out before the measured run starts. Required for read
# tests: without it fio reads sparse or absent files and measures nothing.
prepare:
  enabled: false

# Emit machine-readable results alongside the human-readable output. The
# parser consumes the JSON; the text is for eyeballing a single pod.
fioOutput:
  json: true
  timeSeries: true
```

- [ ] **Step 2: החלף את בלוק ה־args ב־pods.yaml**

החלף את `command`/`args` הנוכחיים ב־[templates/pods.yaml](templates/pods.yaml) בבלוק הבא:

```yaml
    command: ["/bin/bash", "-c"]
    args:
      - |
        set -o pipefail
        echo "pod {{ $i }} of {{ $.Values.replicaCount }}, run {{ $.Values.runId }}"
        cp /fio-config/fio.job /tmp/fio.job
        sed -i 's|directory=.*|directory={{ $.Values.mountPath }}|g' /tmp/fio.job
        mkdir -p /tmp/fiolog

        echo "=== environment ==="
        echo "node=${NODE_NAME} pod=${POD_NAME} fio=$(fio --version)"
        df -h {{ $.Values.mountPath }}
        grep "{{ $.Values.mountPath }}" /proc/mounts || true

        echo "=== fio job ==="
        cat /tmp/fio.job

        {{- if $.Values.startEpoch }}
        # Absolute-time barrier. Every pod in every release of this run waits
        # for the same instant, so a burst phase actually overlaps the phase
        # it is supposed to burst against rather than starting whenever helm
        # happened to return.
        TARGET={{ $.Values.startEpoch }}
        NOW=$(date +%s)
        if [ "$NOW" -lt "$TARGET" ]; then
          echo "barrier: waiting $((TARGET - NOW))s until $(date -d @$TARGET)"
          while [ "$(date +%s)" -lt "$TARGET" ]; do sleep 0.2; done
        else
          echo "barrier: WARNING started $((NOW - TARGET))s late; overlap is not guaranteed"
        fi
        {{- end }}

        echo "=== fio start $(date -u +%Y-%m-%dT%H:%M:%SZ) epoch=$(date +%s) ==="
        {{- if $.Values.fioOutput.json }}
        fio /tmp/fio.job --output-format=json+ --output=/tmp/fio.json
        FIO_RC=$?
        {{- else }}
        fio /tmp/fio.job
        FIO_RC=$?
        {{- end }}
        echo "=== fio end $(date -u +%Y-%m-%dT%H:%M:%SZ) epoch=$(date +%s) rc=$FIO_RC ==="

        {{- if $.Values.fioOutput.json }}
        # Markers let collect_results.sh extract the JSON from kubectl logs
        # without needing a shared volume or a sidecar.
        echo "===FIO_JSON_BEGIN==="
        cat /tmp/fio.json
        echo "===FIO_JSON_END==="
        {{- end }}

        {{- if $.Values.fioOutput.timeSeries }}
        echo "===FIO_TSLOG_BEGIN==="
        for l in /tmp/fiolog/*.log; do
          [ -f "$l" ] || continue
          echo "--- $(basename "$l") ---"
          cat "$l"
        done
        echo "===FIO_TSLOG_END==="
        {{- end }}

        exit $FIO_RC
    env:
    - name: NODE_NAME
      valueFrom: { fieldRef: { fieldPath: spec.nodeName } }
    - name: POD_NAME
      valueFrom: { fieldRef: { fieldPath: metadata.name } }
```

`exit $FIO_RC` הוא השינוי הקטן והחשוב: כרגע ה־container מסיים ב־0 גם כשה־fio נכשל, ולכן `kubectl get pods` מראה `Completed` על ריצה שמתה מ־ENOSPC.

- [ ] **Step 3: הוסף את ה־initContainer שמכין את ה־dataset**

הוסף ל־`spec` ב־[templates/pods.yaml](templates/pods.yaml), לפני `containers:`:

```yaml
  {{- if $.Values.prepare.enabled }}
  initContainers:
  - name: fio-prepare
    image: "{{ $.Values.image.repository }}:{{ $.Values.image.tag }}"
    imagePullPolicy: {{ $.Values.image.pullPolicy }}
    command: ["/bin/bash", "-c"]
    args:
      - |
        set -e
        cp /fio-config/fio.job /tmp/prep.job
        sed -i 's|directory=.*|directory={{ $.Values.mountPath }}|g' /tmp/prep.job
        # create_only lays the files out at full size without measuring, so
        # the measured run reads a real dataset instead of sparse holes.
        echo "laying out dataset (this is not measured)"
        fio /tmp/prep.job --create_only=1 --allow_file_create=1
        echo "dataset ready:"
        du -sh {{ $.Values.mountPath }}
    volumeMounts:
    - name: fio-data
      mountPath: {{ $.Values.mountPath }}
    - name: fio-config
      mountPath: /fio-config
    resources:
      {{- toYaml $.Values.resources | nindent 6 }}
  {{- end }}
```

- [ ] **Step 4: הפעל prepare עבור בדיקות ה־read**

הוסף ל־`deploy_test.sh` בתוך `deploy_release`, אחרי `preflight`:

```bash
  # Read tests need their dataset laid out first; test8 and the burst read
  # phase 2 declare allow_file_create=0 and will refuse to run without it.
  local prep="false"
  case "$job_id" in
    test8_1pod_max_read_4kb|test11_burst_read_phase2) prep="true" ;;
  esac
```

והוסף `--set prepare.enabled="$prep"` לקריאת `helm_deploy`.

- [ ] **Step 5: אמת rendering של ה־barrier**

```bash
helm template t . -n fio-tests --set runId=r1 --set startEpoch=1789000000 --set replicaCount=1 | grep -A3 "barrier: waiting"
```

Expected: הבלוק מופיע עם `TARGET=1789000000`.

- [ ] **Step 6: אמת שבלי startEpoch אין barrier**

```bash
helm template t . -n fio-tests --set runId=r1 --set replicaCount=1 | grep -c "barrier"
```

Expected: `0`

- [ ] **Step 7: אמת rendering של prepare**

```bash
helm template t . -n fio-tests --set runId=r1 --set prepare.enabled=true --set replicaCount=1 | grep -A2 "name: fio-prepare"
```

Expected: ה־initContainer מופיע עם ה־image הנכון.

- [ ] **Step 8: Commit**

```bash
git add templates/ values.yaml scripts/deploy_test.sh && git commit -m "feat: absolute-time start barrier, dataset prepare, json+ and time-series output, propagate fio exit code"
```

---

## Task 6: משאבי לקוח — שהרעב לא יהיה בצד הפוד

`resources.limits.cpu: 2000m` עם `numjobs=8`, `iodepth=32` ו־`refill_buffers=1` הופך את הפוד לחסם. בדיקה שמדדה את מגבלת ה־CPU של הלקוח אינה בדיקת אחסון — וכשה־CFS מצנן את הקונטיינר, ה־throttling מופיע ב־fio כ־tail latency שנראה בדיוק כמו latency של המערך.

Task זה עושה שלושה דברים: מעלה את המשאבים לפי סוג הבדיקה, **מוכיח** שהלקוח לא היה החסם באמצעות מוני throttling של ה־cgroup, ומוסיף שער קיבולת שרץ בתוך הפוד — כך שגם `helm install` ידני בלי הסקריפט אינו יכול לשחזר את ENOSPC של test4.

**Files:**
- Modify: `values.yaml`
- Modify: `templates/pods.yaml`
- Modify: `scripts/lib/common.sh`, `scripts/deploy_test.sh`

**Interfaces:**
- Produces: `===FIO_CGROUP_BEGIN===` בלוג — נצרך ב־Task 8 לפסילת ריצה שנחנקה
- Produces: `resource_class_for()` — נצרך ב־Task 3 ו־Task 7

- [ ] **Step 1: הגדר שלוש מחלקות משאבים**

ברירת מחדל אחת אינה מתאימה גם ל־100 IOPS וגם ל־10 GiB/s. הוסף ל־[values.yaml](values.yaml) והחלף את בלוק `resources` הקיים:

```yaml
# Resource classes. requests == limits on purpose: that gives the pod
# Guaranteed QoS, which is the only class the kubelet will not throttle
# first under node pressure. A throttled fio client reports CFS stalls as
# storage latency, and there is no way to tell the two apart after the fact.
#
#   light   rate-limited, small block   (test_example)
#   normal  rate-limited, up to ~5K IOPS or ~3 GiB/s per pod
#   heavy   ceiling tests and >=5 GiB/s (test7, test8, test9, test10, test11)
resources:
  requests:
    memory: "4Gi"
    cpu: "4"
  limits:
    memory: "4Gi"
    cpu: "4"
```

- [ ] **Step 2: הוסף את בורר המחלקה לסקריפט**

הוסף ל־`scripts/lib/common.sh`:

```bash
# resource_class_for <test_id> -> "<cpu> <memory>"
#
# Sized from the observed ceiling run: test7 sustained ~170K IOPS / 665 MiB/s
# of 4K random writes with refill_buffers=1, i.e. two thirds of a GiB per
# second of freshly generated incompressible data. That is CPU work, and at
# 2 cores it is the client that gives out first, not the array.
resource_class_for() {
  case "$1" in
    test_example*)
      echo "1 1Gi" ;;
    *1pod_max*|test10_burst*|test11_burst*)
      echo "8 8Gi" ;;
    *)
      echo "4 4Gi" ;;
  esac
}
```

והוסף ל־`deploy_release` ב־`deploy_test.sh`, אחרי `preflight`:

```bash
  read -r _cpu _mem <<< "$(resource_class_for "$job_id")"
```

ולקריאת `helm_deploy`:

```bash
    --set resources.requests.cpu="$_cpu" --set resources.limits.cpu="$_cpu" \
    --set resources.requests.memory="$_mem" --set resources.limits.memory="$_mem" \
```

הסר את ה־`--set resources.requests.cpu=100m --set resources.requests.memory=128Mi` הידניים ממסלול `test_example` — מחלקת `light` מכסה אותם.

- [ ] **Step 3: הוסף שער קיבולת שרץ בתוך הפוד**

זהו התיקון שמגן גם על `helm install` ידני. הסקריפט בודק קיבולת לפני הפריסה (Task 2), אבל מי שמריץ helm ישירות עוקף אותו. הבדיקה הזו רצה בתוך הקונטיינר, אחרי שה־PVC כבר mounted, ולכן היא רואה את הקיבולת האמיתית ולא את מה שהוצהר.

הוסף ל־`args` ב־[templates/pods.yaml](templates/pods.yaml), אחרי ה־`sed` שמתקן את ה־directory ולפני ה־barrier:

```bash
        # Capacity gate. size is per clone, so the dataset is size x numjobs.
        # Failing here costs seconds; failing inside fio costs the whole run
        # and produces a half-dead log that looks like a result.
        NUMJOBS=$(grep -oP '^\s*numjobs\s*=\s*\K[0-9]+' /tmp/fio.job | head -1)
        SECTIONS=$(grep -c '^\[' /tmp/fio.job)
        SECTIONS=$((SECTIONS > 1 ? SECTIONS - 1 : 1))
        if [ -n "$NUMJOBS" ]; then CLONES=$((NUMJOBS * SECTIONS)); else CLONES=$SECTIONS; fi
        SIZE_RAW=$(grep -oP '^\s*size\s*=\s*\K[0-9]+[KMGTkmgt]?' /tmp/fio.job | head -1)
        SIZE_NUM=$(echo "$SIZE_RAW" | grep -oE '^[0-9]+')
        case "$(echo "$SIZE_RAW" | grep -oE '[KMGTkmgt]$' | tr 'a-z' 'A-Z')" in
          K) MULT=1 ;; M) MULT=1024 ;; G) MULT=$((1024*1024)) ;;
          T) MULT=$((1024*1024*1024)) ;; *) MULT=1 ;;
        esac
        NEED_KB=$((SIZE_NUM * MULT * CLONES))
        AVAIL_KB=$(df -Pk {{ $.Values.mountPath }} | awk 'NR==2 {print $4}')
        echo "capacity: dataset needs $((NEED_KB/1024/1024)) GiB (size=${SIZE_RAW} x ${CLONES} clones), mount has $((AVAIL_KB/1024/1024)) GiB free"
        if [ "$NEED_KB" -gt "$AVAIL_KB" ]; then
          echo "FATAL: PVC is too small for this job."
          echo "  fio would lay out files until it hit ENOSPC, kill part of the"
          echo "  workload, and still exit having produced a plausible-looking log."
          echo "  Increase pvc.size to at least $(( (NEED_KB * 12 / 10) / 1024 / 1024 ))Gi and re-run."
          exit 28
        fi

        # cgroup CPU pressure, before. If the client is throttled during the
        # run then its latency figures describe the CFS scheduler, not NFS.
        if [ -r /sys/fs/cgroup/cpu.stat ]; then
          CG_BEFORE=$(cat /sys/fs/cgroup/cpu.stat)
        elif [ -r /sys/fs/cgroup/cpu/cpu.stat ]; then
          CG_BEFORE=$(cat /sys/fs/cgroup/cpu/cpu.stat)
        else
          CG_BEFORE=""
        fi
```

- [ ] **Step 4: דווח throttling אחרי הריצה**

הוסף ל־`args`, מיד אחרי שורת `=== fio end ... ===`:

```bash
        echo "===FIO_CGROUP_BEGIN==="
        echo "before: ${CG_BEFORE}"
        if [ -r /sys/fs/cgroup/cpu.stat ]; then
          echo "after: $(cat /sys/fs/cgroup/cpu.stat)"
        elif [ -r /sys/fs/cgroup/cpu/cpu.stat ]; then
          echo "after: $(cat /sys/fs/cgroup/cpu/cpu.stat)"
        fi
        echo "===FIO_CGROUP_END==="
```

- [ ] **Step 5: פסול ריצה שנחנקה**

הוסף ל־`scripts/lib/fiojson.py` (Task 8):

```python
CGROUP_BEGIN = "===FIO_CGROUP_BEGIN==="
CGROUP_END = "===FIO_CGROUP_END==="


def parse_cgroup_throttling(log_text):
    """Return microseconds of CPU throttling during the run, or None.

    nr_throttled counts periods in which the cgroup exhausted its quota and
    was stalled to the end of the period. Any non-zero value means some of
    the latency fio measured was the scheduler, not the storage.
    """
    if CGROUP_BEGIN not in log_text or CGROUP_END not in log_text:
        return None
    block = log_text.split(CGROUP_BEGIN, 1)[1].split(CGROUP_END, 1)[0]
    vals = {}
    for tag in ("before", "after"):
        seg = ""
        for line in block.splitlines():
            if line.startswith(tag + ":"):
                seg = line.split(":", 1)[1]
        for token in seg.replace("\n", " ").split():
            if token.isdigit() and prev in ("nr_throttled", "throttled_usec", "throttled_time"):
                vals.setdefault(tag, {})[prev] = int(token)
            prev = token
    if "before" not in vals or "after" not in vals:
        return None
    key = "throttled_usec" if "throttled_usec" in vals["after"] else "throttled_time"
    return vals["after"].get(key, 0) - vals["before"].get(key, 0)
```

והוסף ל־`validate_run`:

```python
        thr = getattr(p, "throttled_usec", None)
        if thr:
            v.fail(f"{p.pod}: CPU cgroup throttled for {thr / 1e6:.1f}s during the run; "
                   f"the client was the bottleneck, not the storage. "
                   f"Raise the resource class and re-run.")
```

- [ ] **Step 6: הוסף anti-affinity**

הוסף ל־[values.yaml](values.yaml):

```yaml
# Two benchmark pods on one worker share its NIC and page cache, which shows
# up as array latency that is not array latency. Preferred, not required, so
# a 40-pod test can still schedule on a smaller cluster.
podAntiAffinity:
  enabled: true
```

ובתוך `spec` ב־[templates/pods.yaml](templates/pods.yaml):

```yaml
  {{- if $.Values.podAntiAffinity.enabled }}
  affinity:
    podAntiAffinity:
      preferredDuringSchedulingIgnoredDuringExecution:
      - weight: 100
        podAffinityTerm:
          topologyKey: kubernetes.io/hostname
          labelSelector:
            matchLabels:
              fio.benchmark/run-id: {{ $.Values.runId | quote }}
  {{- else }}
  {{- with $.Values.affinity }}
  affinity:
    {{- toYaml . | nindent 4 }}
  {{- end }}
  {{- end }}
```

הסר את בלוק ה־`{{- with $.Values.affinity }}` המקורי שבסוף ה־spec, אחרת יירנדרו שני מפתחות `affinity`.

- [ ] **Step 7: אמת ש־QoS הוא Guaranteed**

```bash
helm template t . -n fio-tests --set runId=r1 --set replicaCount=1 | grep -A6 "resources:"
```

Expected: `requests` ו־`limits` זהים לחלוטין ב־cpu וב־memory — זה מה שמקנה Guaranteed QoS.

- [ ] **Step 8: אמת שאין כפילות affinity**

```bash
helm template t . -n fio-tests --set runId=r1 --set replicaCount=1 | grep -c "^  affinity:"
```

Expected: `1`

- [ ] **Step 9: אמת ששער הקיבולת עובד**

```bash
helm template t . -n fio-tests --set runId=r1 --set replicaCount=1 | grep -c "FATAL: PVC is too small"
```

Expected: `1`

- [ ] **Step 10: אמת את חישוב הקיבולת מול קובץ אמיתי**

```bash
docker run --rm -v "$(pwd)/jobs:/jobs" rafmoshe2500/fio:3.41 bash -c '
  cp /jobs/tests/test4_10pods_50k_5050_32kb.fio /tmp/fio.job
  NUMJOBS=$(grep -oP "^\s*numjobs\s*=\s*\K[0-9]+" /tmp/fio.job | head -1)
  SECTIONS=$(grep -c "^\[" /tmp/fio.job); SECTIONS=$((SECTIONS-1))
  echo "clones=$((NUMJOBS * SECTIONS)) expected=8"'
```

Expected: `clones=8 expected=8` — הבדיקה שנכשלה ב־ENOSPC הייתה נחסמת כאן.

- [ ] **Step 11: Commit**

```bash
git add values.yaml templates/pods.yaml scripts/ && git commit -m "fix: tiered Guaranteed-QoS resources, in-pod capacity gate, cgroup throttling detection"
```

---

## Task 7: gradual-scale — מדרגות במקום זחילה

הסקריפט מוסיף 5 פודים בדקה במשך 30 דקות, בעוד כל פוד מריץ 1,800 שניות מרגע יצירתו. הפודים המוקדמים מסיימים כשהמאוחרים מתחילים; אין רגע שבו 160 פודים רצים יחד, וכל summary מייצג concurrency אחר.

**Files:**
- Create: `scripts/deploy_scale_steps.sh`
- Modify: `jobs/tests/test1[2-6]_gradual_scale_*.fio`

**Interfaces:**
- Consumes: `helm_deploy()`, `preflight()`, `pvc_size_for()` מ־Tasks 1 ו־3
- Produces: `results/<run_id>/steps.json` — מיפוי מדרגה→חלון זמן, נצרך ב־Task 9

- [ ] **Step 1: קצר את ה־runtime לחלון מדידה אחד**

בכל אחד מ־`test12`–`test16`, החלף `runtime=1800` ב־:

```ini
runtime=420
ramp_time=60
```

חלון מדידה של 6 דקות אחרי דקת warm-up, שמסתיים מלא לפני שהמדרגה הבאה מתחילה. `runtime=1800` היה תלוי בכך שכל הפודים מתחילים יחד — הנחה שלא התקיימה מעולם.

- [ ] **Step 2: כתוב את ה־orchestrator המדורג**

`scripts/deploy_scale_steps.sh`:

```bash
#!/bin/bash
# Stepped scaling: each replica count gets its own complete run -- prepare,
# warm-up, measurement window, full drain -- before the next step starts.
#
# The old approach added five pods a minute while every pod ran a 30-minute
# job, so no two pods ever measured the same concurrency. A scaling curve
# needs a stable window per point, not a moving target.
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

TEST_ID="$1"
NAMESPACE="$2"
RUN_ID="$3"

SAFE_TEST_ID="$(safe_id "$TEST_ID")"
STORAGE_CLASS="${STORAGE_CLASS:-sc-nas-nfs3}"
STEPS="${STEPS:-10 20 40 80}"
BARRIER_LEAD="${BARRIER_LEAD:-180}"

RESULTS_DIR="$CHART_DIR/results/$RUN_ID"
mkdir -p "$RESULTS_DIR"

preflight "$TEST_ID"
PVC="$(pvc_size_for "$TEST_ID")"
JOB="$(job_file_for "$TEST_ID")"

echo '{"steps":[' > "$RESULTS_DIR/steps.json"
first=true

for n in $STEPS; do
  release="fio-${SAFE_TEST_ID}-s${n}"
  release="${release:0:53}"
  start_epoch=$(( $(date +%s) + BARRIER_LEAD ))

  log "step $n pods: deploying $release, start at $(date -d "@$start_epoch")"
  helm_deploy "$release" "$NAMESPACE" "$RUN_ID" \
    --set replicaCount="$n" \
    --set namePrefix="fio-${SAFE_TEST_ID}-s${n}" \
    --set startEpoch="$start_epoch" \
    --set pvc.storageClassName="$STORAGE_CLASS" \
    --set pvc.size="$PVC" \
    --set-file fioJob.content="$JOB"

  log "step $n: waiting for all $n pods to reach a terminal state"
  local_deadline=$(( start_epoch + 600 ))
  while true; do
    done_count=$(kubectl get pods -n "$NAMESPACE" \
      -l "$(run_selector "$RUN_ID"),pod-index" \
      --field-selector "status.phase!=Running,status.phase!=Pending" \
      -o name 2>/dev/null | wc -l)
    [ "$done_count" -ge "$n" ] && break
    [ "$(date +%s)" -gt "$local_deadline" ] && { warn "step $n timed out with $done_count/$n done"; break; }
    sleep 15
  done

  "$CHART_DIR/scripts/collect_results.sh" "$RUN_ID" "$NAMESPACE" "step-$n"

  $first || echo ',' >> "$RESULTS_DIR/steps.json"
  first=false
  printf '{"replicas":%d,"release":"%s","start_epoch":%d,"end_epoch":%d}' \
    "$n" "$release" "$start_epoch" "$(date +%s)" >> "$RESULTS_DIR/steps.json"

  log "step $n: tearing down before the next step"
  helm uninstall "$release" -n "$NAMESPACE" || true
  kubectl delete pvc -n "$NAMESPACE" -l "$(run_selector "$RUN_ID")" --wait=true || true
done

echo ']}' >> "$RESULTS_DIR/steps.json"
log "scaling curve complete: $RESULTS_DIR/steps.json"
```

- [ ] **Step 3: אמת תחביר**

```bash
bash -n scripts/deploy_scale_steps.sh && echo "syntax ok"
```

Expected: `syntax ok`

- [ ] **Step 4: אמת שה־JSON תקין**

```bash
python3 -c "import json; json.loads('{\"steps\":[{\"replicas\":10,\"release\":\"a\",\"start_epoch\":1,\"end_epoch\":2}\n,\n{\"replicas\":20,\"release\":\"b\",\"start_epoch\":3,\"end_epoch\":4}]}')" && echo "json shape ok"
```

Expected: `json shape ok`

- [ ] **Step 5: Commit**

```bash
git add scripts/deploy_scale_steps.sh jobs/tests/ && git commit -m "fix: stepped scaling with a stable measurement window per replica count"
```

---

## Task 8: parser יחיד על JSON עם ולידציה קשיחה

זהו ה־Task שהיה מונע את דוח ה־PASS על test4. כל ערך חסר הוא `None`, לא `0`; ריצה שחסרים בה כיוונים מוצהרים, runtime או bytes — נפסלת.

**Files:**
- Create: `scripts/lib/fiojson.py`, `scripts/lib/testmeta.py`
- Create: `scripts/tests/test_fiojson.py`, `scripts/tests/test_testmeta.py`
- Create: `scripts/tests/fixtures/*.json`

**Interfaces:**
- Consumes: `jobs/tests/*.meta.json` מ־Task 4
- Produces: `PodResult`, `RunValidation`, `load_meta()` — נצרכים ב־Task 9

- [ ] **Step 1: צור fixtures מייצגים**

`scripts/tests/fixtures/ok_mixed.json` — ריצת 50/50 תקינה:

```json
{
  "fio version": "fio-3.41",
  "jobs": [{
    "jobname": "test1-30k-5050-4k",
    "error": 0,
    "elapsed": 601,
    "read":  {"io_bytes": 73700000000, "bw_bytes": 122800000, "iops": 29980.5,
              "runtime": 600005, "iops_stddev": 91.2,
              "clat_ns": {"mean": 2391300, "stddev": 2486710,
                          "percentile": {"50.000000": 2000000, "95.000000": 4100000,
                                         "99.000000": 9000000, "99.900000": 21000000}}},
    "write": {"io_bytes": 73700000000, "bw_bytes": 122800000, "iops": 29975.1,
              "runtime": 600005, "iops_stddev": 88.7,
              "clat_ns": {"mean": 2386340, "stddev": 2794350,
                          "percentile": {"50.000000": 2000000, "95.000000": 4100000,
                                         "99.000000": 9200000, "99.900000": 22000000}}},
    "usr_cpu": 1.33, "sys_cpu": 6.89, "ctx": 32065615
  }]
}
```

`scripts/tests/fixtures/enospc_write_dead.json` — בדיוק מה שקרה ב־test4:

```json
{
  "fio version": "fio-3.41",
  "jobs": [{
    "jobname": "test4_50k_5050",
    "error": 28,
    "elapsed": 601,
    "read":  {"io_bytes": 999999000000, "bw_bytes": 1666000000, "iops": 50840.0,
              "runtime": 600024, "iops_stddev": 140.0,
              "clat_ns": {"mean": 620000, "stddev": 310000,
                          "percentile": {"50.000000": 600000, "95.000000": 900000,
                                         "99.000000": 1100000, "99.900000": 2000000}}},
    "write": {"io_bytes": 0, "bw_bytes": 0, "iops": 0.0, "runtime": 0,
              "iops_stddev": 0.0,
              "clat_ns": {"mean": 0, "stddev": 0, "percentile": {}}},
    "usr_cpu": 2.1, "sys_cpu": 11.4, "ctx": 41000000
  }]
}
```

`scripts/tests/fixtures/truncated.json` — לוג שנקטע:

```json
{"fio version": "fio-3.41", "jobs": [{"jobname": "x", "error": 0
```

`scripts/tests/fixtures/nsec_latency.json` — התקן מהיר, latency ב־ns נמוך:

```json
{
  "fio version": "fio-3.41",
  "jobs": [{
    "jobname": "fast",
    "error": 0,
    "elapsed": 61,
    "read": {"io_bytes": 4096000000, "bw_bytes": 68000000, "iops": 16601.0,
             "runtime": 60000, "iops_stddev": 12.0,
             "clat_ns": {"mean": 48000, "stddev": 9000,
                         "percentile": {"50.000000": 45000, "95.000000": 62000,
                                        "99.000000": 88000, "99.900000": 150000}}},
    "write": {"io_bytes": 0, "bw_bytes": 0, "iops": 0.0, "runtime": 0,
              "iops_stddev": 0.0, "clat_ns": {"mean": 0, "stddev": 0, "percentile": {}}},
    "usr_cpu": 4.0, "sys_cpu": 12.0, "ctx": 900000
  }]
}
```

- [ ] **Step 2: כתוב את הטסטים הכושלים**

`scripts/tests/test_fiojson.py`:

```python
import json
import os
import unittest

from lib.fiojson import parse_pod_json, validate_run, MissingMetric

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def _fixture(name):
    with open(os.path.join(FIX, name)) as fh:
        return fh.read()


class ParsePodJsonTest(unittest.TestCase):
    def test_healthy_mixed_run(self):
        r = parse_pod_json("pod-0", _fixture("ok_mixed.json"))
        self.assertEqual(r.error, 0)
        self.assertAlmostEqual(r.read.iops, 29980.5)
        self.assertAlmostEqual(r.read.clat_p99_ms, 9.0)
        self.assertAlmostEqual(r.write.clat_p99_ms, 9.2)
        self.assertEqual(r.directions_with_io, {"read", "write"})

    def test_nanoseconds_convert_to_milliseconds_not_passed_through(self):
        r = parse_pod_json("pod-0", _fixture("nsec_latency.json"))
        self.assertAlmostEqual(r.read.clat_mean_ms, 0.048)
        self.assertAlmostEqual(r.read.clat_p99_ms, 0.088)

    def test_absent_direction_is_none_not_zero(self):
        r = parse_pod_json("pod-0", _fixture("nsec_latency.json"))
        self.assertIsNone(r.write.clat_p99_ms)
        self.assertIsNone(r.write.clat_mean_ms)
        self.assertEqual(r.directions_with_io, {"read"})

    def test_truncated_json_raises(self):
        with self.assertRaises(MissingMetric):
            parse_pod_json("pod-0", _fixture("truncated.json"))


class ValidateRunTest(unittest.TestCase):
    def _meta(self, **over):
        m = {"test_id": "t", "replicas": 1, "expected_directions": ["read", "write"],
             "runtime_s": 600, "rate_limited": True}
        m.update(over)
        return m

    def test_enospc_run_is_rejected(self):
        """The test4 case: fio error 28, write side produced nothing, and the
        old parser called it PASS."""
        r = parse_pod_json("pod-0", _fixture("enospc_write_dead.json"))
        v = validate_run([r], self._meta())
        self.assertFalse(v.ok)
        self.assertIn("fio error 28", " ".join(v.failures))
        self.assertIn("no I/O in expected direction 'write'", " ".join(v.failures))

    def test_healthy_run_passes(self):
        r = parse_pod_json("pod-0", _fixture("ok_mixed.json"))
        v = validate_run([r], self._meta())
        self.assertTrue(v.ok, v.failures)

    def test_missing_pod_is_rejected(self):
        r = parse_pod_json("pod-0", _fixture("ok_mixed.json"))
        v = validate_run([r], self._meta(replicas=10))
        self.assertFalse(v.ok)
        self.assertIn("expected 10 pods, got 1", " ".join(v.failures))

    def test_read_only_meta_accepts_read_only_run(self):
        r = parse_pod_json("pod-0", _fixture("nsec_latency.json"))
        v = validate_run([r], self._meta(expected_directions=["read"], runtime_s=60))
        self.assertTrue(v.ok, v.failures)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: הרץ וודא שנכשל**

```bash
cd scripts && python3 -m unittest tests.test_fiojson -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'lib.fiojson'`

- [ ] **Step 4: כתוב את המימוש**

`scripts/lib/fiojson.py`:

```python
"""Parse fio's json+ output into a strict, validated result.

The rule this module enforces: a metric that fio did not report is None, not
zero. The previous text parser initialised every field to 0.0, so a run in
which the entire write half died with ENOSPC produced read_p99=..., write
everything=0, and every zero fell below every warning threshold. Ten failed
pods were reported as four PASS and six WARN.
"""

import json
from dataclasses import dataclass, field


class MissingMetric(Exception):
    """Raised when the JSON is absent, truncated or missing a required key."""


NS_PER_MS = 1_000_000.0
BYTES_PER_MIB = 1024.0 ** 2


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


@dataclass
class DirectionResult:
    name: str
    iops: float = None
    bw_mibps: float = None
    io_bytes: int = None
    runtime_ms: int = None
    iops_stddev: float = None
    clat_mean_ms: float = None
    clat_stddev_ms: float = None
    clat_p50_ms: float = None
    clat_p95_ms: float = None
    clat_p99_ms: float = None
    clat_p999_ms: float = None

    @property
    def had_io(self):
        return bool(self.io_bytes) and bool(self.runtime_ms)


@dataclass
class PodResult:
    pod: str
    error: int = 0
    elapsed_s: int = None
    read: DirectionResult = None
    write: DirectionResult = None
    usr_cpu: float = None
    sys_cpu: float = None
    ctx_switches: int = None

    @property
    def directions_with_io(self):
        return {d.name for d in (self.read, self.write) if d and d.had_io}


def _direction(name, blob):
    """Build a DirectionResult. A direction fio reported with zero bytes is
    recorded as 'present but idle' -- io_bytes 0, latencies None -- so the
    caller can tell 'did not run' from 'ran fast'."""
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
        raise MissingMetric(f"{pod}: fio JSON is absent or truncated: {e}")

    jobs = doc.get("jobs")
    if not jobs:
        raise MissingMetric(f"{pod}: fio JSON has no 'jobs' array")

    # group_reporting=1 collapses every clone into one entry. If a job file
    # ever drops it, take the first and let validation catch the mismatch.
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


@dataclass
class RunValidation:
    ok: bool = True
    failures: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def fail(self, msg):
        self.ok = False
        self.failures.append(msg)

    def warn(self, msg):
        self.warnings.append(msg)


def validate_run(pods, meta):
    """Decide whether this run may be reported at all.

    A run that fails here produces no tables and no scores. Reporting numbers
    from a partially dead run is the failure mode this whole plan exists to
    remove.
    """
    v = RunValidation()
    expected = set(meta.get("expected_directions") or [])

    want_pods = int(meta.get("replicas", 0) or 0)
    if want_pods and len(pods) != want_pods:
        v.fail(f"expected {want_pods} pods, got {len(pods)}")

    for p in pods:
        if p.error:
            v.fail(f"{p.pod}: fio error {p.error} "
                   f"({'ENOSPC - PVC too small for size x numjobs' if p.error == 28 else 'see the pod log'})")

        missing = expected - p.directions_with_io
        for d in sorted(missing):
            v.fail(f"{p.pod}: no I/O in expected direction '{d}'")

        extra = p.directions_with_io - expected
        for d in sorted(extra):
            v.warn(f"{p.pod}: unexpected I/O in direction '{d}'")

        for d in (p.read, p.write):
            if d and d.had_io and d.clat_p99_ms is None:
                v.fail(f"{p.pod}: {d.name} has I/O but no clat percentiles; "
                       f"was the log truncated?")

        want_runtime = meta.get("runtime_s")
        if want_runtime and p.elapsed_s:
            # ramp_time is excluded from runtime but included in elapsed.
            floor = want_runtime * 0.9
            if p.elapsed_s < floor:
                v.fail(f"{p.pod}: ran {p.elapsed_s}s, expected at least {floor:.0f}s")

    return v
```

- [ ] **Step 5: הרץ וודא שעובר**

```bash
cd scripts && python3 -m unittest tests.test_fiojson -v
```

Expected: `Ran 8 tests ... OK`

- [ ] **Step 6: כתוב את מודול המטא־דאטה עם טסט**

`scripts/tests/test_testmeta.py`:

```python
import json
import os
import tempfile
import unittest

from lib.testmeta import load_meta, MetaError


class LoadMetaTest(unittest.TestCase):
    def _write(self, obj):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "t1.meta.json")
        with open(path, "w") as fh:
            json.dump(obj, fh)
        return d

    def test_loads_valid_meta(self):
        d = self._write({"test_id": "t1", "replicas": 10, "numjobs": 8,
                         "expected_directions": ["read", "write"],
                         "runtime_s": 600, "rate_limited": True,
                         "target_iops_per_pod": 3000})
        m = load_meta("t1", d)
        self.assertEqual(m["replicas"], 10)

    def test_missing_file_is_an_error_not_a_default(self):
        with self.assertRaises(MetaError) as cm:
            load_meta("nope", tempfile.mkdtemp())
        self.assertIn("no metadata", str(cm.exception))

    def test_missing_required_field_rejected(self):
        d = self._write({"test_id": "t1", "replicas": 10})
        with self.assertRaises(MetaError) as cm:
            load_meta("t1", d)
        self.assertIn("expected_directions", str(cm.exception))

    def test_bad_direction_rejected(self):
        d = self._write({"test_id": "t1", "replicas": 1, "numjobs": 1,
                         "expected_directions": ["sideways"],
                         "runtime_s": 60, "rate_limited": False})
        with self.assertRaises(MetaError):
            load_meta("t1", d)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 7: הרץ וודא שנכשל, אז כתוב את המימוש**

```bash
cd scripts && python3 -m unittest tests.test_testmeta -v
```

Expected: FAIL — `ModuleNotFoundError`

`scripts/lib/testmeta.py`:

```python
"""Load the declared intent of a test.

Profiles used to be guessed from a directory name, which classified 512K
random as sequential and a 70/30 job as 50/50, then scored the run against
targets it was never meant to hit. Intent is declared, not inferred.
"""

import json
import os

REQUIRED = ("test_id", "replicas", "numjobs",
            "expected_directions", "runtime_s", "rate_limited")
VALID_DIRECTIONS = {"read", "write"}


class MetaError(Exception):
    pass


def load_meta(test_id, meta_dir):
    path = os.path.join(meta_dir, f"{test_id}.meta.json")
    if not os.path.exists(path):
        raise MetaError(
            f"no metadata for '{test_id}' at {path}.\n"
            f"  Every test declares its targets in jobs/tests/<test_id>.meta.json.\n"
            f"  Without it there is nothing to score the run against."
        )
    try:
        with open(path) as fh:
            meta = json.load(fh)
    except ValueError as e:
        raise MetaError(f"{path}: invalid JSON: {e}")

    for key in REQUIRED:
        if key not in meta:
            raise MetaError(f"{path}: missing required field '{key}'")

    bad = set(meta["expected_directions"]) - VALID_DIRECTIONS
    if bad:
        raise MetaError(f"{path}: unknown direction(s) {sorted(bad)}; "
                        f"valid: {sorted(VALID_DIRECTIONS)}")
    if not meta["expected_directions"]:
        raise MetaError(f"{path}: expected_directions must not be empty")

    return meta
```

- [ ] **Step 8: הרץ את כל הטסטים**

```bash
cd scripts && python3 -m unittest discover -s tests -v
```

Expected: `Ran 17 tests ... OK`

- [ ] **Step 9: Commit**

```bash
git add scripts/lib/fiojson.py scripts/lib/testmeta.py scripts/tests/ && git commit -m "feat: strict json+ parser - missing metrics are None, broken runs are rejected"
```

---

## Task 9: דיווח — percentiles אמיתיים ובלי ציונים מומצאים

ממוצע של P99 בין פודים אינו P99 של הקלאסטר. ציוני A/S ואבחנות כמו "NFS lock contention" מוצגים כעובדה על סמך fio בלבד.

**Files:**
- Create: `scripts/lib/report.py`, `scripts/tests/test_report.py`
- Modify: `scripts/parse_results.py` (שכתוב מלא)
- Delete: `scripts/parse_results_v2.py`

**Interfaces:**
- Consumes: `PodResult`, `RunValidation` מ־Task 8; `load_meta` מ־Task 8

- [ ] **Step 1: כתוב את הטסט הכושל**

`scripts/tests/test_report.py`:

```python
import unittest

from lib.fiojson import DirectionResult, PodResult
from lib.report import cluster_percentile, aggregate


def _pod(name, r_iops, r_p99, w_iops, w_p99):
    return PodResult(
        pod=name, error=0, elapsed_s=600,
        read=DirectionResult("read", iops=r_iops, io_bytes=1, runtime_ms=600000,
                             bw_mibps=r_iops * 0.004, clat_p99_ms=r_p99),
        write=DirectionResult("write", iops=w_iops, io_bytes=1, runtime_ms=600000,
                              bw_mibps=w_iops * 0.004, clat_p99_ms=w_p99),
    )


class ClusterPercentileTest(unittest.TestCase):
    def test_reports_worst_pod_not_the_mean(self):
        """One pod at 400ms among nine at 5ms is the number that matters.
        The mean, 44.5ms, hides it."""
        pods = [_pod(f"p{i}", 1000, 5.0, 1000, 5.0) for i in range(9)]
        pods.append(_pod("p9", 1000, 400.0, 1000, 400.0))
        self.assertEqual(cluster_percentile(pods, "read", "clat_p99_ms"), 400.0)

    def test_ignores_pods_without_the_metric(self):
        pods = [_pod("p0", 1000, 5.0, 1000, 5.0)]
        pods.append(PodResult(pod="p1", read=DirectionResult("read"),
                              write=DirectionResult("write")))
        self.assertEqual(cluster_percentile(pods, "read", "clat_p99_ms"), 5.0)

    def test_none_when_no_pod_has_it(self):
        pods = [PodResult(pod="p0", read=DirectionResult("read"),
                          write=DirectionResult("write"))]
        self.assertIsNone(cluster_percentile(pods, "read", "clat_p99_ms"))


class AggregateTest(unittest.TestCase):
    def test_iops_sum_and_target_attainment(self):
        pods = [_pod(f"p{i}", 1500, 5.0, 1500, 5.0) for i in range(10)]
        meta = {"replicas": 10, "target_iops_total": 30000,
                "expected_directions": ["read", "write"]}
        agg = aggregate(pods, meta)
        self.assertEqual(agg["read_iops_total"], 15000)
        self.assertEqual(agg["write_iops_total"], 15000)
        self.assertAlmostEqual(agg["iops_attainment_pct"], 100.0)

    def test_no_attainment_without_a_declared_target(self):
        pods = [_pod("p0", 1500, 5.0, 1500, 5.0)]
        agg = aggregate(pods, {"replicas": 1, "expected_directions": ["read", "write"]})
        self.assertIsNone(agg["iops_attainment_pct"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: הרץ וודא שנכשל**

```bash
cd scripts && python3 -m unittest tests.test_report -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'lib.report'`

- [ ] **Step 3: כתוב את המימוש**

`scripts/lib/report.py`:

```python
"""Turn validated pod results into something reportable.

Two deliberate omissions: there are no letter grades, and there are no
diagnoses. A grade implies a calibrated SLO, and this suite has none yet.
A claim like 'NFS lock contention' cannot be made from fio output alone --
it needs array-side telemetry. Both come back in P2, after calibration.
"""

import csv
import json

BYTES_PER_MIB = 1024.0 ** 2


def cluster_percentile(pods, direction, attr):
    """The cluster's tail is the worst pod's tail, not the average of the
    pods' tails. Averaging P99 across ten pods turns one pod at 400ms into
    a reported 44ms and hides the only pod anyone cares about.

    A true merged percentile needs the clat_ns histogram bins from json+;
    see merged_percentile() below for that. This is the honest headline.
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
    across pods and walking to the q-th sample gives the real distribution,
    which max-of-p99 only approximates.
    """
    merged = {}
    for bins in pod_bins:
        for ns, count in (bins or {}).items():
            key = float(ns)
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
    2,000-IOPS-per-pod job against a 15,000-IOPS profile.
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
        "read_p99_worst_ms": cluster_percentile(pods, "read", "clat_p99_ms"),
        "write_p99_worst_ms": cluster_percentile(pods, "write", "clat_p99_ms"),
        "read_p999_worst_ms": cluster_percentile(pods, "read", "clat_p999_ms"),
        "write_p999_worst_ms": cluster_percentile(pods, "write", "clat_p999_ms"),
        "iops_attainment_pct": (
            100.0 * (r_iops + w_iops) / target_iops if target_iops else None),
        "bw_attainment_pct": (
            100.0 * (r_bw + w_bw) / target_bw if target_bw else None),
    }


def _fmt(v, spec=".1f"):
    return "n/a" if v is None else format(v, spec)


def print_report(pods, meta, agg, validation):
    w = 96
    print("=" * w)
    print(f"  {meta['test_id']}   {len(pods)} pods   "
          f"{'rate-limited' if meta.get('rate_limited') else 'ceiling (uncapped)'}")
    print("=" * w)

    if not validation.ok:
        print()
        print("  RUN REJECTED - no metrics are reported for a run that did not complete")
        for f in validation.failures:
            print(f"    FAIL  {f}")
        print()
        print("=" * w)
        return

    for msg in validation.warnings:
        print(f"  warn  {msg}")

    print()
    print(f"{'pod':<28} {'R-IOPS':>10} {'W-IOPS':>10} "
          f"{'R-MiB/s':>10} {'W-MiB/s':>10} {'R-P99ms':>9} {'W-P99ms':>9}")
    print("-" * w)
    for p in sorted(pods, key=lambda x: x.pod):
        print(f"{p.pod:<28} "
              f"{_fmt(p.read.iops, '10.0f')} {_fmt(p.write.iops, '10.0f')} "
              f"{_fmt(p.read.bw_mibps, '10.1f')} {_fmt(p.write.bw_mibps, '10.1f')} "
              f"{_fmt(p.read.clat_p99_ms, '9.2f')} {_fmt(p.write.clat_p99_ms, '9.2f')}")
    print("-" * w)
    print(f"{'CLUSTER TOTAL':<28} "
          f"{agg['read_iops_total']:>10.0f} {agg['write_iops_total']:>10.0f} "
          f"{agg['read_bw_mibps_total']:>10.1f} {agg['write_bw_mibps_total']:>10.1f} "
          f"{_fmt(agg['read_p99_worst_ms'], '9.2f')} "
          f"{_fmt(agg['write_p99_worst_ms'], '9.2f')}")
    print()
    print("  P99 columns on the CLUSTER row are the worst pod, not an average.")

    if agg["iops_attainment_pct"] is not None:
        print(f"  IOPS attainment: {agg['iops_attainment_pct']:.1f}% of the declared "
              f"{meta['target_iops_total']:,} total")
    if agg["bw_attainment_pct"] is not None:
        print(f"  Bandwidth attainment: {agg['bw_attainment_pct']:.1f}% of the declared "
              f"{meta['target_bw_mibps_total']:,} MiB/s total")
    if agg["iops_attainment_pct"] is None and agg["bw_attainment_pct"] is None:
        print("  No target declared for this test; figures are a ceiling, not a verdict.")
    print("=" * w)


def write_csv(path, pods, agg):
    cols = ["pod", "read_iops", "write_iops", "read_mibps", "write_mibps",
            "read_clat_mean_ms", "write_clat_mean_ms",
            "read_p50_ms", "read_p95_ms", "read_p99_ms", "read_p999_ms",
            "write_p50_ms", "write_p95_ms", "write_p99_ms", "write_p999_ms",
            "usr_cpu", "sys_cpu", "ctx_switches"]
    with open(path, "w", newline="") as fh:
        wr = csv.writer(fh)
        wr.writerow(cols)
        for p in sorted(pods, key=lambda x: x.pod):
            wr.writerow([
                p.pod, p.read.iops, p.write.iops, p.read.bw_mibps, p.write.bw_mibps,
                p.read.clat_mean_ms, p.write.clat_mean_ms,
                p.read.clat_p50_ms, p.read.clat_p95_ms, p.read.clat_p99_ms, p.read.clat_p999_ms,
                p.write.clat_p50_ms, p.write.clat_p95_ms, p.write.clat_p99_ms, p.write.clat_p999_ms,
                p.usr_cpu, p.sys_cpu, p.ctx_switches,
            ])


def write_json(path, meta, agg, validation, pods):
    with open(path, "w") as fh:
        json.dump({
            "test_id": meta["test_id"],
            "valid": validation.ok,
            "failures": validation.failures,
            "warnings": validation.warnings,
            "aggregate": agg,
            "pods": [{
                "pod": p.pod, "error": p.error, "elapsed_s": p.elapsed_s,
                "read_iops": p.read.iops, "write_iops": p.write.iops,
                "read_p99_ms": p.read.clat_p99_ms, "write_p99_ms": p.write.clat_p99_ms,
            } for p in sorted(pods, key=lambda x: x.pod)],
        }, fh, indent=2)
```

- [ ] **Step 4: הרץ וודא שעובר**

```bash
cd scripts && python3 -m unittest tests.test_report -v
```

Expected: `Ran 5 tests ... OK`

- [ ] **Step 5: שכתב את ה־CLI**

`scripts/parse_results.py`:

```python
#!/usr/bin/env python3
"""Parse a collected fio benchmark run and report it -- or refuse to.

    ./parse_results.py results/test1-...-20260909-120000

Exit codes:
    0  run valid, report printed
    2  run rejected: incomplete, errored, or missing required metrics
    3  could not load the run at all
"""

import argparse
import glob
import json
import os
import sys

from lib.fiojson import MissingMetric, parse_pod_json, validate_run
from lib.report import aggregate, print_report, write_csv, write_json
from lib.testmeta import MetaError, load_meta

JSON_BEGIN = "===FIO_JSON_BEGIN==="
JSON_END = "===FIO_JSON_END==="


def extract_json(log_text, pod):
    """Pull the json+ block out of a pod log. The human-readable fio output
    is still in the log above it; we deliberately do not parse that."""
    if JSON_BEGIN not in log_text:
        raise MissingMetric(
            f"{pod}: no {JSON_BEGIN} marker in the log.\n"
            f"  The pod ran without --output-format=json+, or was collected "
            f"before it finished.")
    body = log_text.split(JSON_BEGIN, 1)[1]
    if JSON_END not in body:
        raise MissingMetric(f"{pod}: log truncated between the JSON markers")
    return body.split(JSON_END, 1)[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("results_dir")
    ap.add_argument("--meta-dir", default=None,
                    help="defaults to <repo>/jobs/tests")
    a = ap.parse_args()

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    meta_dir = a.meta_dir or os.path.join(repo, "jobs", "tests")

    manifest_path = os.path.join(a.results_dir, "manifest.json")
    if not os.path.exists(manifest_path):
        sys.exit(f"{a.results_dir}: no manifest.json.\n"
                 f"  Runs are only reportable when deploy_test.sh recorded "
                 f"what was deployed.")
    with open(manifest_path) as fh:
        manifest = json.load(fh)

    try:
        meta = load_meta(manifest["test_id"], meta_dir)
    except MetaError as e:
        sys.exit(str(e))

    logs = sorted(glob.glob(os.path.join(a.results_dir, "*.log")))
    if not logs:
        sys.exit(f"no .log files in {a.results_dir}")

    pods, load_failures = [], []
    for path in logs:
        pod = os.path.basename(path)[:-4]
        with open(path, errors="replace") as fh:
            text = fh.read()
        try:
            pods.append(parse_pod_json(pod, extract_json(text, pod)))
        except MissingMetric as e:
            load_failures.append(str(e))

    validation = validate_run(pods, meta)
    for f in load_failures:
        validation.fail(f)

    agg = aggregate(pods, meta) if pods else {}
    print_report(pods, meta, agg, validation)

    if validation.ok:
        write_csv(os.path.join(a.results_dir, "summary_report.csv"), pods, agg)
        write_json(os.path.join(a.results_dir, "summary_report.json"),
                   meta, agg, validation, pods)
        print(f"\nwrote summary_report.csv and summary_report.json "
              f"to {a.results_dir}")
        return 0

    print(f"\nNo summary was written. Fix the run, do not report these numbers.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 6: מחק את ה־parser הכפול**

```bash
git rm scripts/parse_results_v2.py
```

הפונקציונליות ששווה לשמר — P50/P95/P99/P99.9 ו־cross-pod analysis — עברה ל־`report.py` על בסיס JSON. הציונים, ה־grades וה־`nfs_diagnosis` יורדים בכוונה עד כיול מול SLO וטלמטריה, לפי Task 21.

- [ ] **Step 7: הרץ את כל הטסטים**

```bash
cd scripts && python3 -m unittest discover -s tests -v
```

Expected: `Ran 22 tests ... OK`

- [ ] **Step 8: Commit**

```bash
git add scripts/ && git commit -m "feat: single parser with worst-pod percentiles, declared targets, no invented grades"
```

---

## Task 10: איסוף תוצאות scoped ומאומת

`grep "fio-"` אוסף כל פוד תואם ב־namespace — כולל ריצות ישנות ומקבילות. אין המתנה, אין בדיקת exit code, וכשל בפוד אחד עוצר את התהליך ומשאיר תוצאה חלקית.

**Files:**
- Modify: `scripts/collect_results.sh` (שכתוב מלא)

**Interfaces:**
- Consumes: `run_selector()` מ־Task 1; `manifest.json` מ־Task 3
- Produces: `results/<run_id>/*.log`, `pods.json`, `events.txt`, `collection.json`

- [ ] **Step 1: שכתב את הסקריפט**

`scripts/collect_results.sh`:

```bash
#!/bin/bash
set -uo pipefail   # deliberately not -e: one bad pod must not abort collection

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

RUN_ID="${1:-}"
NAMESPACE="${2:-fio-tests}"
SUBDIR="${3:-}"

if [ -z "$RUN_ID" ]; then
  echo "Usage: ./collect_results.sh <run_id> [namespace] [subdir]"
  exit 1
fi

RESULTS_DIR="$CHART_DIR/results/$RUN_ID${SUBDIR:+/$SUBDIR}"
mkdir -p "$RESULTS_DIR"
SEL="$(run_selector "$RUN_ID")"

log "collecting run $RUN_ID from namespace $NAMESPACE"

# Wait for every pod to reach a terminal state. Collecting a Running pod
# yields a truncated log that the parser will reject anyway; waiting is
# cheaper than a rerun.
DEADLINE=$(( $(date +%s) + ${COLLECT_TIMEOUT:-3600} ))
while true; do
  TOTAL=$(kubectl get pods -n "$NAMESPACE" -l "$SEL" -o name 2>/dev/null | wc -l)
  ACTIVE=$(kubectl get pods -n "$NAMESPACE" -l "$SEL" \
    --field-selector 'status.phase=Running' -o name 2>/dev/null | wc -l)
  PENDING=$(kubectl get pods -n "$NAMESPACE" -l "$SEL" \
    --field-selector 'status.phase=Pending' -o name 2>/dev/null | wc -l)
  [ "$TOTAL" -eq 0 ] && die "no pods carry label $SEL in $NAMESPACE"
  [ $((ACTIVE + PENDING)) -eq 0 ] && break
  [ "$(date +%s)" -gt "$DEADLINE" ] && { warn "timed out with $ACTIVE running, $PENDING pending"; break; }
  printf '\r  waiting: %d running, %d pending, %d total' "$ACTIVE" "$PENDING" "$TOTAL"
  sleep 15
done
echo

# Environment first: the logs are worthless without knowing what produced them.
kubectl get pods -n "$NAMESPACE" -l "$SEL" -o json > "$RESULTS_DIR/pods.json"
kubectl get events -n "$NAMESPACE" --sort-by=.lastTimestamp > "$RESULTS_DIR/events.txt" 2>/dev/null
kubectl get pvc -n "$NAMESPACE" -l "$SEL" -o json > "$RESULTS_DIR/pvcs.json" 2>/dev/null
kubectl get nodes -o json > "$RESULTS_DIR/nodes.json" 2>/dev/null

FAILED=0; COLLECTED=0; RESTARTED=0; BADEXIT=0
PODS=$(kubectl get pods -n "$NAMESPACE" -l "$SEL" -o jsonpath='{.items[*].metadata.name}')

for POD in $PODS; do
  if kubectl logs "$POD" -n "$NAMESPACE" > "$RESULTS_DIR/${POD}.log" 2>/dev/null; then
    COLLECTED=$((COLLECTED + 1))
  else
    warn "could not read logs for $POD"
    FAILED=$((FAILED + 1))
    continue
  fi

  # A pod that restarted ran fio twice; its numbers describe neither run.
  RC=$(kubectl get pod "$POD" -n "$NAMESPACE" \
    -o jsonpath='{.status.containerStatuses[0].state.terminated.exitCode}' 2>/dev/null)
  RS=$(kubectl get pod "$POD" -n "$NAMESPACE" \
    -o jsonpath='{.status.containerStatuses[0].restartCount}' 2>/dev/null)
  RSN=$(kubectl get pod "$POD" -n "$NAMESPACE" \
    -o jsonpath='{.status.containerStatuses[0].state.terminated.reason}' 2>/dev/null)

  [ "${RS:-0}" -gt 0 ] 2>/dev/null && { warn "$POD restarted ${RS}x"; RESTARTED=$((RESTARTED + 1)); }
  if [ -n "${RC:-}" ] && [ "$RC" != "0" ]; then
    warn "$POD exited $RC (${RSN:-unknown})"
    BADEXIT=$((BADEXIT + 1))
  fi
done

EXPECTED=$(kubectl get pods -n "$NAMESPACE" -l "$SEL" -o name | wc -l)

cat > "$RESULTS_DIR/collection.json" <<JSON
{
  "run_id": "$RUN_ID",
  "namespace": "$NAMESPACE",
  "collected_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "pods_expected": $EXPECTED,
  "pods_collected": $COLLECTED,
  "pods_log_failed": $FAILED,
  "pods_restarted": $RESTARTED,
  "pods_bad_exit": $BADEXIT
}
JSON

# Copy the deploy manifest alongside step results so each subdir is complete.
[ -n "$SUBDIR" ] && cp "$CHART_DIR/results/$RUN_ID/manifest.json" "$RESULTS_DIR/" 2>/dev/null

log "collected $COLLECTED/$EXPECTED pods to $RESULTS_DIR"
if [ $((FAILED + RESTARTED + BADEXIT)) -gt 0 ]; then
  warn "run is NOT clean: $FAILED unreadable, $RESTARTED restarted, $BADEXIT bad exit"
  warn "parse_results.py will reject it; that is the intended behaviour"
fi
log "report with: python3 scripts/parse_results.py $RESULTS_DIR"
```

- [ ] **Step 2: אמת תחביר**

```bash
bash -n scripts/collect_results.sh && echo "syntax ok"
```

Expected: `syntax ok`

- [ ] **Step 3: אמת שהבחירה היא label־based ולא grep**

```bash
grep -c 'grep "fio-"' scripts/collect_results.sh; grep -c "run_selector" scripts/collect_results.sh
```

Expected: `0` ואז `1` — אין יותר בחירה לפי שם.

- [ ] **Step 4: Commit**

```bash
git add scripts/collect_results.sh && git commit -m "fix: label-scoped collection with terminal-state wait, exit-code and restart validation"
```

---

**סוף P0.** בנקודה זו: הפריסה מגיעה ל־namespace הנכון בשמות חוקיים, PVC מספיק גדול או שהריצה מסורבת, ה־rate תואם לכוונה, ההתחלה מסונכרנת, והדוח נפסל כשהריצה פגומה. הבדיקה מ־test4 הייתה נחסמת ב־preflight, ואם בכל זאת הייתה נכשלת — נפסלת ב־parser.

---

# P1 — כדי לקבל benchmark שניתן להשוואה

## Task 11: NiFi — למדוד עבודה מצטברת, לא גידול נטו

זו הבעיה המרכזית ב־NiFi. `nififlow.py:424` מחשב throughput מהפרש `contentRepositoryStorageUsage.usedSpaceBytes`. זהו occupancy נטו: כש־content claims נמחקים או ממוחזרים, I/O כבד מאוד יכול להיראות כגידול אפס או שלילי.

**Files:**
- Modify: `nifi/nififlow.py`

**Interfaces:**
- Produces: עמודות CSV `counter_bytes`, `counter_files`, `counter_failures`, `out_files`, `out_bytes` — נצרכות ב־Tasks 13, 14, 17

- [ ] **Step 1: הוסף גילוי סוגי processors**

`nififlow.py` — הוסף למחלקה `Nifi`:

```python
    def available_types(self):
        """Which processor types this build actually has. The flow builder
        adapts rather than failing on a NiFi that ships a different set."""
        out = set()
        for t in self.call("GET", "/flow/processor-types").get("processorTypes", []):
            if t.get("type"):
                out.add(t["type"])
        return out

    def counters(self):
        """NiFi counters are cumulative for the life of the flow, unlike the
        status endpoints whose figures cover a rolling five-minute window.
        This is the only NiFi-native source of total work done."""
        out = {}
        body = self.call("GET", "/counters")
        for c in (body.get("counters", {}) or {}).get("aggregateSnapshot", {}).get("counters", []):
            out[c.get("name", "")] = int(c.get("valueCount", 0) or 0)
        return out

    def reset_counters(self):
        body = self.call("GET", "/counters")
        for c in (body.get("counters", {}) or {}).get("aggregateSnapshot", {}).get("counters", []):
            if c.get("id"):
                self.call("PUT", f"/counters/{c['id']}")
```

- [ ] **Step 2: הוסף את ה־constant ל־UpdateCounter**

מתחת להגדרות הקיימות ב־`nififlow.py:41-44`:

```python
CNT = "org.apache.nifi.processors.standard.UpdateCounter"
```

- [ ] **Step 3: הצהר על המצברים בראש build()**

Tasks 12 ו־13 מוסיפים processors לאותם רשימות. הצהר עליהן פעם אחת, בשורה הראשונה של `build()`, לפני יצירת ה־generator:

```python
def build(n, a):
    # Accumulators shared by the counter, failure and latency wiring below.
    # Declared once here so the three blocks that append to them do not each
    # need to guess whether the list already exists.
    types = n.available_types()
    failure_sources = []
    latency_buckets = []
```

- [ ] **Step 4: חבר את מוני העבודה המצטברת**

ב־`build()`, אחרי יצירת ה־sink (`pid`), לפני `n.invalid()`:

```python
    # Cumulative accounting. Repository occupancy is net growth, so a run
    # that writes 2 TB and reclaims 2 TB reports zero. Counters do not
    # reclaim, so they measure the work rather than the leftovers.
    if CNT in types:
        cnt = n.processor(
            CNT, "LOAD-Count-Success", x + 400, 0,
            {"Counter Name": "flowfiles_completed",
             "Delta": "1"},
            ["success"], a.threads, "0 sec")
        n.connect(pid, cnt, _success_of(n, pid), a.bp_objects, a.bp_size)

        bytes_cnt = n.processor(
            CNT, "LOAD-Count-Bytes", x + 400, 200,
            {"Counter Name": "bytes_completed",
             "Delta": "${fileSize}"},
            ["success"], a.threads, "0 sec")
        n.connect(cnt, bytes_cnt, _success_of(n, cnt), a.bp_objects, a.bp_size)
    else:
        print("  warn: UpdateCounter is not available on this NiFi build; "
              "cumulative byte accounting will fall back to output-directory "
              "sizing only", file=sys.stderr)
```

שים לב: `pid` כבר לא יכול להיות auto-terminated על `success` כשה־counter מחובר. שנה את יצירת ה־`PUT` ב־`build()` כך שתעביר `["failure"]` בלבד ל־`autoterm` כאשר `CNT in types` — Task 12 מטפל ב־failure בכל מקרה.

- [ ] **Step 5: הוסף את המדדים ל־snapshot**

ב־`_snapshot()`, לפני ה־`return`:

```python
    counters = {}
    try:
        counters = n.counters()
    except NifiError:
        pass
```

והוסף ל־dict המוחזר:

```python
        "counter_files": counters.get("flowfiles_completed", 0),
        "counter_bytes": counters.get("bytes_completed", 0),
        "counter_failures": counters.get("flowfiles_failed", 0),
```

הוסף את שלוש העמודות ל־`CSV_COLS` (`nififlow.py:295`), אחרי `"prov_used"`.

- [ ] **Step 6: החלף את חישוב ה־throughput ב־summarize()**

ב־`summarize()`, החלף את בלוק `rates` (`nififlow.py:420-424`):

```python
        # Cumulative counters, not repository deltas. A run that writes 2 TB
        # and reclaims 2 TB has content_used delta ~0 and counter_bytes 2 TB;
        # the second number is the throughput, the first is the retention.
        d_bytes = last["counter_bytes"] - first["counter_bytes"]
        d_files = last["counter_files"] - first["counter_files"]
        d_fail = last["counter_failures"] - first["counter_failures"]

        rates = []
        for a_, b_ in zip(s, s[1:]):
            dt = b_["ts"] - a_["ts"]
            if dt > 0:
                rates.append(max(b_["counter_bytes"] - a_["counter_bytes"], 0) / dt)
```

ובבלוק ההדפסה, החלף את `content repo` ככותרת ה־throughput:

```python
        print(f"\n{node}")
        print(f"  work completed  {_fmt(d_bytes):>12} in {d_files:,} FlowFiles   "
              f"mean {_fmt(d_bytes/span):>11}/s")
        if d_fail:
            print(f"  FAILURES        {d_fail:,} FlowFiles failed - this run is not valid")
        print(f"  content repo    {_fmt(d_content):>12} net growth "
              f"(retention, NOT throughput)")
        print(f"  provenance      {_fmt(d_prov):>12} net growth")
        print(f"  flowfile repo   {_fmt(d_ff):>12} net growth")
```

- [ ] **Step 7: עדכן את הכיתוב בסוף summarize()**

החלף את שלוש שורות ההסבר (`nififlow.py:462-464`):

```python
    print("Throughput above is cumulative completed work from NiFi counters.")
    print("Repository figures are net occupancy: they measure retention, not")
    print("work, and will read near zero on a run whose claims were reclaimed.")
    print("Cross-check the byte total against the output directory listing and")
    print("against the array's own counters before quoting either number.")
```

- [ ] **Step 8: אמת compile**

```bash
python3 -m py_compile nifi/nififlow.py && echo "compile ok"
```

Expected: `compile ok`

- [ ] **Step 9: אמת שהעמודות נוספו**

```bash
python3 nifi/nififlow.py header
```

Expected: השורה כוללת `counter_files,counter_bytes,counter_failures`.

- [ ] **Step 10: Commit**

```bash
git add nifi/nififlow.py && git commit -m "fix: measure NiFi throughput from cumulative counters, not repository occupancy"
```

---

## Task 12: NiFi — לספור כשלים במקום לבלוע אותם

`PutFile` ו־`ReplaceText` מבצעים auto-terminate ל־failure. מערכת שאינה מצליחה לכתוב נראית "יציבה" משום שהתור אינו גדל.

**Files:**
- Modify: `nifi/nififlow.py`
- Modify: `nifi/nifi-nfs-loadtest.sh`

**Interfaces:**
- Consumes: `CNT`, `available_types()` מ־Task 11
- Produces: `bulletins()`, עמודת `counter_failures`, יציאה לא־אפסית מ־`summarize` על כשל

- [ ] **Step 1: נתב failure ל־counter במקום auto-terminate**

ב־`build()`, החלף את יצירת ה־`RPL` וה־`PUT` כך ש־`failure` לא מסתיים אוטומטית. הוסף לפני החזרת `build()`:

```python
    # failure used to be auto-terminated everywhere, so a run that could not
    # write a single byte looked healthy: the queue was empty because the
    # FlowFiles were being dropped, not delivered.
    if CNT in types:
        fail_cnt = n.processor(
            CNT, "LOAD-Count-Failures", x + 400, 400,
            {"Counter Name": "flowfiles_failed", "Delta": "1"},
            ["success"], 1, "0 sec")
        for src, rel in failure_sources:
            n.connect(src, fail_cnt, rel, a.bp_objects, a.bp_size)
```

`failure_sources` כבר מוצהר בראש `build()` (Task 11 Step 3). מלא אותו תוך כדי הבנייה: בלולאת ה־rewrites הוסף `failure_sources.append((rid, "failure"))` מיד אחרי יצירת כל `rid`, ואחרי יצירת ה־`PutFile` הוסף `failure_sources.append((pid, "failure"))`. במקביל החלף את ארגומנט ה־`autoterm` שלהם מ־`["failure"]` ומ־`["success", "failure"]` ל־`[]`, אחרת NiFi ימשיך להשליך את ה־FlowFiles לפני שהמונה רואה אותם.

- [ ] **Step 2: הוסף קריאת bulletins**

הוסף למחלקה `Nifi`:

```python
    def bulletins(self, after=0):
        """Bulletins are NiFi's own error surface. A run with unhandled
        bulletins is a run whose numbers describe a broken flow."""
        body = self.call("GET", f"/flow/bulletin-board?after={after}")
        out = []
        for b in (body.get("bulletinBoard", {}) or {}).get("bulletins", []):
            bl = b.get("bulletin") or {}
            if bl.get("level") in ("ERROR", "WARNING"):
                out.append({
                    "id": b.get("id", 0),
                    "level": bl.get("level"),
                    "source": bl.get("sourceName", ""),
                    "message": (bl.get("message") or "")[:200],
                })
        return out
```

- [ ] **Step 3: הוסף פעולת `bulletins` ל־CLI**

ב־`main()`, הוסף `"bulletins"` ל־`choices` ואת הענף:

```python
    elif a.action == "bulletins":
        for b in n.bulletins():
            print(f"{a.label}\t{b['level']}\t{b['source']}\t{b['message']}")
```

- [ ] **Step 4: פסול ריצה עם כשלים ב־summarize()**

בסוף `summarize()`, לפני ההסבר:

```python
    if tot_failures:
        print()
        print("!" * 74)
        print(f"RUN INVALID: {tot_failures:,} FlowFiles failed across the run.")
        print("Throughput figures from a run with processor failures describe")
        print("a flow that was dropping work, not a storage system keeping up.")
        print("!" * 74)
        return 2
    return 0
```

וב־`main()`, החלף `summarize(...)` ב־`sys.exit(summarize(...) or 0)`.

- [ ] **Step 5: אסוף bulletins בסוף כל הקלטה**

ב־`nifi-nfs-loadtest.sh`, בסוף `cmd_record()` לפני `cmd_summary`:

```bash
  log "collecting bulletins"
  : > "${csv%.csv}-bulletins.tsv"
  for ((i=0; i<REPLICAS; i++)); do
    nfy "$i" bulletins >> "${csv%.csv}-bulletins.tsv" 2>/dev/null || true
  done
  if [ -s "${csv%.csv}-bulletins.tsv" ]; then
    warn "$(wc -l < "${csv%.csv}-bulletins.tsv") bulletins recorded - see ${csv%.csv}-bulletins.tsv"
  fi
```

- [ ] **Step 6: אמת compile ותחביר**

```bash
python3 -m py_compile nifi/nififlow.py && bash -n nifi/nifi-nfs-loadtest.sh && echo "ok"
```

Expected: `ok`

- [ ] **Step 7: Commit**

```bash
git add nifi/ && git commit -m "fix: count NiFi processor failures and bulletins, invalidate runs that dropped work"
```

---

## Task 13: NiFi — latency

נאספים נפחים, queue depth, heap ו־GC, אך לא latency של processor ולא end-to-end. בדיקת `dd oflag=dsync` ב־smoke אינה נשמרת ואינה מייצגת tail.

**Files:**
- Modify: `nifi/nififlow.py`

- [ ] **Step 1: אסוף processing time מ־status**

ב־`_snapshot()`, אחרי קריאת `agg`:

```python
    # processingNanos is cumulative per processor since the flow started, so
    # dividing the delta by the task-count delta gives mean task duration over
    # the interval rather than over all time.
    proc_nanos = proc_tasks = 0
    for snap in agg.get("processorStatusSnapshots", []) or []:
        ps = snap.get("processorStatusSnapshot") or {}
        proc_nanos += int(ps.get("processingNanos", 0) or 0)
        proc_tasks += int(ps.get("taskCount", 0) or 0)
```

והוסף ל־dict:

```python
        "proc_nanos": proc_nanos,
        "proc_tasks": proc_tasks,
```

הוסף את שתי העמודות ל־`CSV_COLS`.

- [ ] **Step 2: הוסף דליי end-to-end מבוסס attribute**

ב־`build()`, הוסף `UpdateAttribute` מיד אחרי ה־generator שמטביע חותמת זמן:

```python
    stamp = n.processor(
        UPD, "LOAD-Stamp", 200, 0,
        {"gen_ts": "${now():toNumber()}"},
        [], a.threads, "0 sec")
    n.connect(gen, stamp, _success_of(n, gen), a.bp_objects, a.bp_size)
    prev, x = stamp, 400
```

ולפני ה־sink, אם `CNT` זמין, הוסף דלי latency אחד לכל טווח. `latency_buckets` מוצהר בראש `build()` (Task 11 Step 3):

```python
    # Bucketed end-to-end latency without external telemetry: each bucket is
    # its own cumulative counter, so the CSV carries a coarse histogram.
    if CNT in types:
        y = 600
        for label, lo, hi in (("lt10ms", 0, 10), ("lt50ms", 10, 50),
                              ("lt250ms", 50, 250), ("lt1s", 250, 1000),
                              ("ge1s", 1000, 10 ** 9)):
            b = n.processor(
                CNT, f"LOAD-Lat-{label}", x + 400, y,
                {"Counter Name": f"lat_{label}", "Delta": "1"},
                ["success"], 1, "0 sec")
            latency_buckets.append((b, lo, hi))
            y += 150
```

וחבר אותם דרך `RouteOnAttribute` על `${now():toNumber():minus(${gen_ts})}`. אם `RouteOnAttribute` אינו זמין ב־build, רשום warn ודלג — המדד נשאר `proc_nanos`.

- [ ] **Step 3: דווח latency ב־summarize()**

בבלוק ההדפסה לכל node:

```python
        d_nanos = last["proc_nanos"] - first["proc_nanos"]
        d_tasks = last["proc_tasks"] - first["proc_tasks"]
        if d_tasks > 0:
            print(f"  task duration   mean {d_nanos / d_tasks / 1e6:.2f} ms "
                  f"over {d_tasks:,} processor tasks")
```

- [ ] **Step 4: אמת**

```bash
python3 -m py_compile nifi/nififlow.py && python3 nifi/nififlow.py header
```

Expected: compile נקי, והכותרת כוללת `proc_nanos,proc_tasks`.

- [ ] **Step 5: Commit**

```bash
git add nifi/nififlow.py && git commit -m "feat: NiFi processor task duration and bucketed end-to-end latency"
```

---

## Task 14: NiFi — percentile של קלאסטר במקום כפל שגוי

`_deployment_stats` מחשב `p95_rate` כ־`_pct(rates,.95) * len(rows)`. זו p95 של node בודד כפול מספר ה־nodes — מספר שאף רגע לא התקיים.

**Files:**
- Modify: `nifi/nififlow.py`

- [ ] **Step 1: בנה סדרת זמן ברמת קלאסטר**

החלף את חישוב `rates` ב־`_deployment_stats()`:

```python
    # The old code took p95 of per-node rates and multiplied by node count,
    # which assumes every node peaks in the same second. Bucket the samples
    # by timestamp, sum across nodes within each bucket, then take the
    # percentile of the actual cluster-wide series.
    buckets = {}
    for node, samples in rows.items():
        s = sorted(samples, key=lambda x: x["ts"])
        for a_, b_ in zip(s, s[1:]):
            dt = b_["ts"] - a_["ts"]
            if dt <= 0:
                continue
            rate = max(b_["counter_bytes"] - a_["counter_bytes"], 0) / dt
            # 10s buckets absorb the per-node sampling skew.
            buckets.setdefault(b_["ts"] // 10, []).append(rate)

    cluster_series = [sum(v) for v in buckets.values()]
```

והחלף את השדה:

```python
        "p95_rate": _pct(cluster_series, .95) if cluster_series else 0,
        "median_rate": _pct(cluster_series, .5) if cluster_series else 0,
```

- [ ] **Step 2: הוסף כותרת מסבירה ב־compare()**

אחרי טבלת ההשוואה:

```python
    print()
    print("p95 is the 95th percentile of the cluster-wide rate series, formed")
    print("by summing per-node rates within aligned 10s buckets. It is not the")
    print("per-node p95 scaled by node count.")
```

- [ ] **Step 3: אמת**

```bash
python3 -m py_compile nifi/nififlow.py && grep -c "len(rows)$" nifi/nififlow.py
```

Expected: compile נקי, ו־`0` מופעים של הכפל השגוי.

- [ ] **Step 4: Commit**

```bash
git add nifi/nififlow.py && git commit -m "fix: cluster p95 from an aligned cluster-wide series, not per-node p95 x node count"
```

---

## Task 15: NFS RPC latency מ־mountstats

`/proc/self/mountstats` נותן, לכל פעולת NFS, מונים מצטברים של ops, retransmits, timeouts, queue time ו־RTT. זהו מקור ה־latency הישיר ביותר ל־NFS, והוא כבר קיים בכל pod.

**Files:**
- Create: `nifi/nfsstat.py`, `nifi/tests/test_nfsstat.py`
- Modify: `nifi/nifi-nfs-loadtest.sh`

- [ ] **Step 1: כתוב את הטסט הכושל**

`nifi/tests/test_nfsstat.py`:

```python
import unittest

from nfsstat import parse_mountstats, delta

SAMPLE = """device 10.0.0.5:/export mounted on /opt/nifi/content_repository with fstype nfs statvers=1.1
\topts:\trw,vers=4.1,rsize=1048576,wsize=1048576,hard,proto=tcp
\tage:\t3600
\tper-op statistics
\t        READ: 1000 1000 0 128000 1024000 500 12000 13000
\t       WRITE: 2000 2000 3 256000 2048000 900 48000 51000
\t      COMMIT: 50 50 0 3200 4000 10 900 950
"""


class ParseMountstatsTest(unittest.TestCase):
    def test_extracts_nfs_version_and_ops(self):
        m = parse_mountstats(SAMPLE)
        mount = m["/opt/nifi/content_repository"]
        self.assertEqual(mount["vers"], "4.1")
        self.assertEqual(mount["ops"]["WRITE"]["ops"], 2000)
        self.assertEqual(mount["ops"]["WRITE"]["timeouts"], 3)
        self.assertEqual(mount["ops"]["WRITE"]["rtt_ms"], 48000)

    def test_delta_yields_mean_rtt_per_op(self):
        a = parse_mountstats(SAMPLE)
        b = parse_mountstats(SAMPLE.replace(
            "WRITE: 2000 2000 3 256000 2048000 900 48000 51000",
            "WRITE: 3000 3000 5 384000 3072000 1400 78000 82000"))
        d = delta(a, b, "/opt/nifi/content_repository")
        self.assertEqual(d["WRITE"]["ops"], 1000)
        self.assertEqual(d["WRITE"]["timeouts"], 2)
        self.assertAlmostEqual(d["WRITE"]["mean_rtt_ms"], 30.0)

    def test_zero_op_delta_has_no_mean(self):
        a = parse_mountstats(SAMPLE)
        d = delta(a, a, "/opt/nifi/content_repository")
        self.assertIsNone(d["WRITE"]["mean_rtt_ms"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: הרץ וודא שנכשל**

```bash
cd nifi && python3 -m unittest tests.test_nfsstat -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'nfsstat'`

- [ ] **Step 3: כתוב את המימוש**

`nifi/nfsstat.py`:

```python
#!/usr/bin/env python3
"""Read NFS RPC statistics out of /proc/self/mountstats.

This is the only NFS latency source that costs nothing and needs no array
access: the client kernel already counts every RPC, its retransmits, its
timeouts and its cumulative round-trip time per operation. Two snapshots
around a measurement window give the mean RTT per op for that window.

Per-op columns (statvers=1.1), in order:
    ops ntrans timeouts bytes_sent bytes_recv queue_ms rtt_ms execute_ms
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
    """Per-op deltas plus the derived mean RTT for the window.

    mean_rtt_ms is None when no operations happened -- dividing by zero ops
    would otherwise report 0.0 ms and read as 'instant' instead of 'idle'.
    """
    a = (before.get(mountpoint) or {}).get("ops", {})
    b = (after.get(mountpoint) or {}).get("ops", {})
    out = {}
    for op in sorted(set(a) | set(b)):
        pa, pb = a.get(op, {}), b.get(op, {})
        d = {k: pb.get(k, 0) - pa.get(k, 0) for k in _FIELDS}
        d["mean_rtt_ms"] = (d["rtt_ms"] / d["ops"]) if d["ops"] > 0 else None
        d["mean_queue_ms"] = (d["queue_ms"] / d["ops"]) if d["ops"] > 0 else None
        d["retrans_pct"] = (100.0 * (d["ntrans"] - d["ops"]) / d["ops"]
                            if d["ops"] > 0 else None)
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
        return

    with open(a.before) as fh:
        before = json.load(fh)
    mounts = [a.mount] if a.mount else sorted(now)
    for m in mounts:
        print(f"=== {m} (NFSv{(now.get(m) or {}).get('vers', '?')}) ===")
        for op, d in delta(before, now, m).items():
            if not d["ops"]:
                continue
            print(f"  {op:<12} {d['ops']:>10,} ops  "
                  f"rtt {d['mean_rtt_ms']:>8.2f} ms  "
                  f"queue {d['mean_queue_ms']:>7.2f} ms  "
                  f"retrans {d['retrans_pct']:>5.2f}%  "
                  f"timeouts {d['timeouts']:>6,}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: הרץ וודא שעובר**

```bash
cd nifi && python3 -m unittest tests.test_nfsstat -v
```

Expected: `Ran 3 tests ... OK`

- [ ] **Step 5: אסוף snapshots סביב חלון המדידה**

ב־`nifi-nfs-loadtest.sh`, הוסף פונקציה והפעל אותה ב־`cmd_record()` לפני ואחרי הלולאה:

```bash
nfsstat_snapshot() { # nfsstat_snapshot <out-file>
  local out="$1" i
  : > "$out"
  for ((i=0; i<REPLICAS; i++)); do
    kubectl -n "$NS" exec "nifi-${i}" -c nifi -- \
      cat /proc/self/mountstats 2>/dev/null \
      | python3 "$(dirname "$HELPER")/nfsstat.py" --file /dev/stdin \
      >> "$out" 2>/dev/null || warn "nifi-${i}: no mountstats"
    echo >> "$out"
  done
}
```

- [ ] **Step 6: Commit**

```bash
git add nifi/nfsstat.py nifi/tests/ nifi/nifi-nfs-loadtest.sh && git commit -m "feat: NFS RPC latency, retransmits and timeouts from mountstats"
```

---

## Task 16: NiFi — lifecycle, barrier ו־apples-to-apples

ה־flows נבנים ומופעלים node אחר node, ההקלטה מתחילה בלי warm-up, ו־`deployments.json` משנה בין deployments גם profile, גם replicas, גם file size וגם `ALWAYS_SYNC` — כך שאי אפשר לייחס הבדל ל־StorageClass.

**Files:**
- Modify: `nifi/nifi-nfs-loadtest.sh`, `nifi/nifi-multi.sh`, `nifi/deployments.json`

- [ ] **Step 1: בנה הכל STOPPED, ואז הפעל בבת אחת**

החלף את `cmd_flow()` ב־`nifi-nfs-loadtest.sh`:

```bash
cmd_flow() {
  forward_all
  local i out_dir=""
  [[ "$WRITE_OUTPUT" == "true" ]] && out_dir='/data/out/${hostname()}'

  # Build every node first, leaving all of them STOPPED. Starting node 0 while
  # node 2 is still being built means node 0 has a head start on the array,
  # which is exactly the skew a comparison must not have.
  for ((i=0; i<REPLICAS; i++)); do
    log "waiting for nifi-${i} REST API"
    nfy "$i" ping --wait "${API_WAIT}" || die "nifi-${i} never became usable"
    log "building flow on nifi-${i} (stopped)"
    nfy "$i" clear || die "nifi-${i}: clear failed; a stale flow would corrupt this run"
    nfy "$i" build \
      --file-size "$FILE_SIZE" --batch "$BATCH_SIZE" --threads "$CONCURRENT" \
      --rewrites "$REWRITES" --schedule "$SCHEDULE" \
      --bp-objects "$BP_OBJECTS" --bp-size "$BP_SIZE" \
      --output-dir "$out_dir" \
      || die "flow build failed on nifi-${i}"
  done

  # Absolute-time barrier, same mechanism as the fio pods.
  local start_at="${START_EPOCH:-$(( $(date +%s) + 30 ))}"
  log "starting all ${REPLICAS} nodes at $(date -d "@$start_at" 2>/dev/null || date -r "$start_at")"
  while [ "$(date +%s)" -lt "$start_at" ]; do sleep 0.2; done
  for ((i=0; i<REPLICAS; i++)); do
    nfy "$i" state --state RUNNING &
  done
  wait
  log "load is live on all nodes"
}
```

הערה: `nfy "$i" clear` כבר לא מובלע ב־`|| true`. flow ישן שלא נמחק הוא state שנשמר בין ריצות — בדיוק מה שהסקירה מזהירה מפניו.

- [ ] **Step 2: הוסף lifecycle מלא**

הוסף ל־`nifi-nfs-loadtest.sh`:

```bash
# The full measured lifecycle. 'record' alone samples whatever state the flow
# happens to be in; this makes the phases explicit and comparable.
cmd_run() {
  local duration="${1:-900}" warmup="${WARMUP:-120}" drain="${DRAIN:-180}"
  cmd_deploy
  cmd_flow
  log "warm-up ${warmup}s (not measured)"
  sleep "$warmup"
  log "resetting counters -- measurement starts from zero"
  local i; for ((i=0; i<REPLICAS; i++)); do nfy "$i" reset-counters || true; done
  cmd_record "$duration"
  log "stopping generators, draining queues for up to ${drain}s"
  cmd_state STOPPED
  local deadline=$(( $(date +%s) + drain ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    local q; q=$(nfy 0 sample | cut -d, -f9)
    [[ "${q:-0}" -eq 0 ]] && { log "queues drained"; break; }
    sleep 10
  done
  cmd_verify
}

# Prove the output matches what the flow claims it produced.
cmd_verify() {
  [[ "$WRITE_OUTPUT" == "true" ]] || { warn "WRITE_OUTPUT=false; nothing to verify"; return 0; }
  local i files bytes
  for ((i=0; i<REPLICAS; i++)); do
    files=$(kubectl -n "$NS" exec "nifi-${i}" -c nifi -- \
      bash -c 'find /data/out -type f | wc -l' 2>/dev/null || echo 0)
    bytes=$(kubectl -n "$NS" exec "nifi-${i}" -c nifi -- \
      bash -c 'du -sb /data/out | cut -f1' 2>/dev/null || echo 0)
    log "nifi-${i}: ${files} output files, ${bytes} bytes on the export"
  done
  log "compare these against counter_files / counter_bytes in the CSV;"
  log "a gap means FlowFiles were dropped or the counters are miscounting"
}
```

הוסף `run) cmd_run "${2:-900}" ;;` ו־`verify) cmd_verify ;;` ל־`case` בסוף, ו־`reset-counters` ל־`choices` ב־`nififlow.py`.

- [ ] **Step 3: הוסף config hash**

הוסף ל־`nifi-nfs-loadtest.sh`:

```bash
# Everything that changes the workload. Two runs whose hashes differ are not
# comparable, no matter how similar the graphs look.
config_hash() {
  printf '%s\n' "$PROFILE" "$FILE_SIZE" "$BATCH_SIZE" "$CONCURRENT" \
    "$REWRITES" "$SCHEDULE" "$ALWAYS_SYNC" "$ARCHIVE_ENABLED" \
    "$CHECKPOINT_INTERVAL" "$MAX_APPENDABLE_SIZE" "$REPLICAS" \
    "$BP_OBJECTS" "$BP_SIZE" "$WRITE_OUTPUT" "$HEAP" "$CPU_LIM" "$MEM_LIM" \
    | sha256sum | cut -c1-16
}
```

וכתוב אותו לצד ה־CSV ב־`cmd_record()`:

```bash
  cat > "${csv%.csv}-manifest.json" <<JSON
{
  "config_hash": "$(config_hash)",
  "storage_class": "$STORAGE_CLASS",
  "namespace": "$NS",
  "replicas": $REPLICAS,
  "profile": "$PROFILE",
  "file_size": "$FILE_SIZE",
  "batch_size": $BATCH_SIZE,
  "concurrent": $CONCURRENT,
  "rewrites": $REWRITES,
  "always_sync": "$ALWAYS_SYNC",
  "archive_enabled": "$ARCHIVE_ENABLED",
  "checkpoint_interval": "$CHECKPOINT_INTERVAL",
  "nifi_image": "$NIFI_IMAGE",
  "started_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "nodes": $(kubectl -n "$NS" get pods -l app=nifi-loadtest -o jsonpath='{range .items[*]}"{.spec.nodeName}",{end}' | sed 's/,$//' | sed 's/^/[/;s/$/]/')
}
JSON
```

- [ ] **Step 4: חסום השוואה בין קונפיגורציות שונות**

ב־`nififlow.py`, ב־`compare()`, טען את ה־manifest לצד כל CSV:

```python
def _manifest_for(csv_path):
    p = csv_path[:-4] + "-manifest.json" if csv_path.endswith(".csv") else ""
    if p and os.path.exists(p):
        with open(p) as fh:
            return json.load(fh)
    return None
```

ובתחילת `compare()`, אחרי איסוף `results`:

```python
    hashes = {}
    for label, path, _ in results:
        man = _manifest_for(path)
        hashes[label] = (man or {}).get("config_hash", "unknown")
    distinct = set(hashes.values()) - {"unknown"}
    if len(distinct) > 1:
        print("!" * w)
        print("NOT COMPARABLE: these runs used different workload configurations.")
        for label, h in sorted(hashes.items()):
            print(f"  {label:12} config {h}")
        print("A comparison set must vary exactly one parameter. Re-run with")
        print("identical workload settings and only the StorageClass changed.")
        print("!" * w)
        print()
```

- [ ] **Step 5: תקן את `deployments.json` להשוואה תקפה**

`nifi/deployments.json`:

```json
{
  "_comment": "A comparison set varies exactly one parameter. Here that is storageClass. Every other setting is identical, including ALWAYS_SYNC -- the previous file enabled sync only on nfs41, which made the protocol comparison meaningless.",
  "defaults": {
    "replicas": 3,
    "profile": "smallfile",
    "env": {
      "WRITE_OUTPUT": "true",
      "CONTENT_REPO_SIZE": "50Gi",
      "PROVENANCE_REPO_SIZE": "20Gi",
      "FLOWFILE_REPO_SIZE": "10Gi",
      "ALWAYS_SYNC": "false",
      "ARCHIVE_ENABLED": "false",
      "CHECKPOINT_INTERVAL": "20 secs",
      "MAX_APPENDABLE_SIZE": "1 MB",
      "FILE_SIZE": "4 KB",
      "BATCH_SIZE": "200",
      "CONCURRENT": "8"
    }
  },
  "deployments": [
    { "name": "nfs3",  "storageClass": "sc-nas-nfs3" },
    { "name": "nfs41", "storageClass": "sc-nas-nfs41" }
  ]
}
```

הפרופילים והגדלים השונים שהיו כאן קודם עוברים לקובץ נפרד, `deployments-profiles.json`, שנועד לסקירת פרופילים ולא להשוואת פרוטוקולים.

- [ ] **Step 6: העבר את ה־barrier המשותף ל־multi**

ב־`nifi-multi.sh`, ב־`cmd_record()`, לפני הלולאה:

```bash
  # One absolute start instant shared by every child, so the deployments are
  # under load simultaneously rather than staggered by their own setup time.
  local start_epoch=$(( $(date +%s) + ${BARRIER_LEAD:-120} ))
  log "synchronised measurement start at $(date -d "@$start_epoch" 2>/dev/null || date -r "$start_epoch")"
```

והוסף `START_EPOCH="$start_epoch"` לרשימת המשתנים ב־`eval env ...`.

- [ ] **Step 7: בודד את מופעי NiFi בין ה־workers**

בלי זה, שני מופעי NiFi יכולים לרוץ על אותו worker ולהתחרות על CPU, זיכרון ו־NIC — והפרש שנראה כמו הבדל בין StorageClasses הוא בעצם שני pods שחלקו כרטיס רשת. הוסף ל־`render()` ב־`nifi-nfs-loadtest.sh`, בתוך `spec:` של ה־Pod template (אחרי `serviceAccountName`):

```yaml
      affinity:
        podAntiAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
          - topologyKey: kubernetes.io/hostname
            labelSelector:
              matchLabels: { app: nifi-loadtest }
      topologySpreadConstraints:
      - maxSkew: 1
        topologyKey: kubernetes.io/hostname
        whenUnsatisfiable: DoNotSchedule
        labelSelector:
          matchLabels: { app: nifi-loadtest }
```

`required`, לא `preferred`: השוואה שבה שני nodes חלקו worker אינה השוואה. אם ה־cluster קטן מדי, ה־pods יישארו Pending — וזה המסר הנכון, לא ריצה שקטה עם מספרים מזוהמים.

- [ ] **Step 8: אמת שה־anti-affinity נרנדר**

```bash
REPLICAS=3 bash nifi/nifi-nfs-loadtest.sh render | grep -c "requiredDuringSchedulingIgnoredDuringExecution"
```

Expected: `1`

- [ ] **Step 9: תקן את איתור ה־CONF**

ב־`nifi-multi.sh` החלף את בלוק בחירת ה־CONF כך שיהיה script-relative:

```bash
if [[ -z "${CONF:-}" ]]; then
  _d="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if   [[ -f "$_d/deployments.json" ]]; then CONF="$_d/deployments.json"
  elif [[ -f "$_d/deployments.conf" ]]; then CONF="$_d/deployments.conf"
  else CONF="$_d/deployments.json"
  fi
fi
```

- [ ] **Step 10: אמת הכל**

```bash
bash -n nifi/nifi-nfs-loadtest.sh && bash -n nifi/nifi-multi.sh && python3 -m py_compile nifi/nififlow.py && python3 -c "import json; json.load(open('nifi/deployments.json'))" && echo "all ok"
```

Expected: `all ok`

- [ ] **Step 11: Commit**

```bash
git add nifi/ && git commit -m "feat: NiFi run lifecycle with barrier, warm-up, drain, verify; config hash guards comparisons"
```

---

## Task 17: manifest, חזרות ורווח סמך

ריצה בודדת אינה מדידה. ללא חזרות אין דרך לדעת אם הפרש של 8% בין שני StorageClasses הוא אמיתי.

**Files:**
- Create: `scripts/run_repeated.sh`, `scripts/lib/stats.py`, `scripts/tests/test_stats.py`

- [ ] **Step 1: כתוב את הטסט הכושל**

`scripts/tests/test_stats.py`:

```python
import unittest

from lib.stats import median, confidence_interval, comparable


class MedianTest(unittest.TestCase):
    def test_odd_and_even(self):
        self.assertEqual(median([3, 1, 2]), 2)
        self.assertEqual(median([4, 1, 3, 2]), 2.5)

    def test_empty_is_none(self):
        self.assertIsNone(median([]))


class ConfidenceIntervalTest(unittest.TestCase):
    def test_three_samples_produce_a_range(self):
        lo, hi = confidence_interval([100.0, 102.0, 98.0])
        self.assertLess(lo, 100.0)
        self.assertGreater(hi, 100.0)

    def test_single_sample_has_no_interval(self):
        self.assertEqual(confidence_interval([100.0]), (None, None))


class ComparableTest(unittest.TestCase):
    def test_overlapping_intervals_are_not_a_difference(self):
        self.assertFalse(comparable([100.0, 101.0, 99.0], [103.0, 104.0, 102.0]))

    def test_separated_intervals_are_a_difference(self):
        self.assertTrue(comparable([100.0, 101.0, 99.0], [200.0, 201.0, 199.0]))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: הרץ וודא שנכשל**

```bash
cd scripts && python3 -m unittest tests.test_stats -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'lib.stats'`

- [ ] **Step 3: כתוב את המימוש**

`scripts/lib/stats.py`:

```python
"""Repeat-run statistics.

The point of this module is to stop a single run being quoted as a result.
Storage benchmarks are noisy; an 8% gap between two StorageClasses means
nothing until you know the spread within each one.
"""

import math

# Two-sided 95% t-values for small samples, indexed by degrees of freedom.
_T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571,
        6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228}


def median(values):
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    mid = len(v) // 2
    return v[mid] if len(v) % 2 else (v[mid - 1] + v[mid]) / 2.0


def confidence_interval(values, level=0.95):
    """95% CI of the mean via the t distribution. Returns (None, None) for a
    single sample, because one measurement has no spread to report."""
    v = [x for x in values if x is not None]
    n = len(v)
    if n < 2:
        return (None, None)
    mean = sum(v) / n
    var = sum((x - mean) ** 2 for x in v) / (n - 1)
    se = math.sqrt(var / n)
    t = _T95.get(n - 1, 1.96)
    return (mean - t * se, mean + t * se)


def comparable(a, b):
    """True when the two sets' 95% intervals do not overlap -- i.e. when the
    difference survives the noise. False means 'do not claim a difference'."""
    a_lo, a_hi = confidence_interval(a)
    b_lo, b_hi = confidence_interval(b)
    if a_lo is None or b_lo is None:
        return False
    return a_hi < b_lo or b_hi < a_lo
```

- [ ] **Step 4: הרץ וודא שעובר**

```bash
cd scripts && python3 -m unittest tests.test_stats -v
```

Expected: `Ran 6 tests ... OK`

- [ ] **Step 5: כתוב את ה־runner החוזר**

`scripts/run_repeated.sh`:

```bash
#!/bin/bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

TEST_ID="${1:-}"
NAMESPACE="${2:-fio-tests}"
REPEATS="${REPEATS:-3}"

[ -n "$TEST_ID" ] || die "Usage: ./run_repeated.sh <test_id> [namespace]"

RUN_IDS=()
for r in $(seq 1 "$REPEATS"); do
  log "=== repetition $r of $REPEATS ==="
  "$CHART_DIR/scripts/deploy_test.sh" "$TEST_ID" "$NAMESPACE"
  RID="$(cat "$CHART_DIR/results/.last_run_id")"
  [ -n "$RID" ] || die "could not determine run id for repetition $r"
  RUN_IDS+=("$RID")
  "$CHART_DIR/scripts/collect_results.sh" "$RID" "$NAMESPACE"
  python3 "$CHART_DIR/scripts/parse_results.py" "$CHART_DIR/results/$RID" \
    || warn "repetition $r was rejected and will be excluded"
  FORCE=true "$CHART_DIR/scripts/cleanup_test.sh" "$RID" "$NAMESPACE"
  # Let the array settle so repetition r+1 does not inherit r's cache state.
  sleep "${SETTLE:-120}"
done

log "aggregating ${#RUN_IDS[@]} repetitions"
# argv[1] is the repo root: `python3 -` leaves __file__ undefined, so the
# path has to be passed in rather than derived.
( cd "$CHART_DIR/scripts" && python3 - "$CHART_DIR" "${RUN_IDS[@]}" ) <<'PY'
import json, os, sys
from lib.stats import median, confidence_interval

repo = sys.argv[1]
iops, valid = [], 0
for rid in sys.argv[2:]:
    path = os.path.join(repo, "results", rid, "summary_report.json")
    if not os.path.exists(path):
        print(f"  {rid}: rejected, excluded")
        continue
    with open(path) as fh:
        d = json.load(fh)
    if not d.get("valid"):
        print(f"  {rid}: invalid, excluded")
        continue
    valid += 1
    iops.append(d["aggregate"]["total_iops"])
    print(f"  {rid}: {d['aggregate']['total_iops']:,.0f} IOPS")

if valid < 2:
    sys.exit("\nFewer than two valid repetitions; no interval can be reported.")
lo, hi = confidence_interval(iops)
print(f"\nmedian {median(iops):,.0f} IOPS   95% CI [{lo:,.0f}, {hi:,.0f}]")
print("Quote the median and the interval. A single run is not a result.")
PY
```

- [ ] **Step 6: אמת**

```bash
bash -n scripts/run_repeated.sh && echo "syntax ok" && cd scripts && python3 -m unittest discover -s tests -v
```

Expected: `syntax ok` ואז `Ran 28 tests ... OK`

- [ ] **Step 7: Commit**

```bash
git add scripts/ && git commit -m "feat: repeated runs with median and 95% confidence intervals"
```

---

**סוף P1.** בנקודה זו כל ריצה נושאת manifest מלא, יש time-series, החזרות מדווחות עם רווח סמך, NiFi מודד עבודה מצטברת עם latency ו־NFS RPC stats, וההשוואות נחסמות כשהקונפיגורציות שונות.

---

# P2 — כדי לטעון שהבדיקה מייצגת NiFi

> **הערה על רמת הפירוט:** בניגוד ל־P0 ו־P1, המשימות כאן מוגדרות ברמת מטרה ולא ברמת קוד — במכוון. התוכן שלהן תלוי במדידה שעדיין לא בוצעה: **Task 21 Step 1** אוסף telemetry מסביבת NiFi אמיתית, וממנה נגזרים גדלי הקבצים, הקצבים ויחסי ה־read/write של Task 18, וכן ערכי ה־SLO של Task 21. לכתוב עכשיו קובצי fio מדויקים לפרופילים "מכוילים" פירושו להמציא את המספרים שהכיול אמור לספק — בדיוק הכשל שהתוכנית מתקנת. **הרץ את Task 21 Step 1 ראשון, ואז הרחב את Tasks 18–22 למפרט מלא לפי סכמת המשימות של P0/P1.**

## Task 18: מטריצת עומסים מכוילת

**Files:** `jobs/profiles/*.fio`, `jobs/profiles/README.md`

- [ ] **Step 1:** צור פרופיל לכל שורה במטריצה — `low_qd_latency`, `target_rate_steady`, `max_throughput`, `cold_read`, `warm_read`, `sync_write`, `smallfile_lifecycle`, `nifi_content`, `nifi_flowfile`, `nifi_provenance`, `overload_drain` — כל אחד עם `.meta.json` מלא לפי סכמת Task 4.
- [ ] **Step 2:** ל־`sync_write` הגדר `fsync=1` ו־`sync=1`, ולא `end_fsync=1` — `end_fsync` מסנכרן פעם אחת בסוף ואינו מדמה את ה־checkpoint המחזורי של NiFi.
- [ ] **Step 3:** ל־`nifi_content`, `nifi_flowfile`, `nifi_provenance` הסר `direct=1` — NiFi אינו משתמש ב־O_DIRECT, ובדיקה שכן משתמשת מודדת מסלול I/O אחר.
- [ ] **Step 4:** הוסף `rate_iops_min` ל־`target_rate_steady` כדי שריצה שלא עמדה ביעד תיכשל ב־fio עצמו ולא רק בדוח.
- [ ] **Step 5:** Commit: `git commit -m "feat: calibrated workload matrix with NiFi-representative buffered profiles"`

## Task 19: פרופיל compressible

**Files:** `jobs/profiles/compressible.fio`, `jobs/profiles/incompressible.fio`

- [ ] **Step 1:** `incompressible.fio` — `refill_buffers=1 scramble_buffers=1 dedupe_percentage=0`
- [ ] **Step 2:** `compressible.fio` — `buffer_compress_percentage=50 buffer_compress_chunk=4096 dedupe_percentage=25 refill_buffers=0`
- [ ] **Step 3:** הרץ את שניהם על אותו StorageClass ותעד את ההפרש. אם הוא גדול, כל מספר קודם היה תלוי בהנחת דחיסות שלא הוצהרה.
- [ ] **Step 4:** Commit: `git commit -m "feat: explicit compressible and incompressible data profiles"`

## Task 20: failure injection ו־recovery

**Files:** `nifi/chaos.sh`

- [ ] **Step 1:** `delete-pod` — מחיקת pod תחת עומס, מדידת זמן עד חזרה ל־RUNNING ועד שהתור מתחיל להתנקז.
- [ ] **Step 2:** `network-fault` — הוספת `NetworkPolicy` שחוסמת את שרת ה־NFS למשך N שניות, ואז הסרתה.
- [ ] **Step 3:** `verify-integrity` — השוואת `counter_files` מול ספירת הקבצים ב־export ומול checksums, ופסילה על כל פער.
- [ ] **Step 4:** תעד ב־`nifi/README.md` שהתוצאה היא `at-least-once` ולא `exactly-once` אלא אם הוכח אחרת.
- [ ] **Step 5:** Commit: `git commit -m "feat: failure injection with recovery timing and integrity verification"`

## Task 21: כיול SLO והחזרת ציונים

**Files:** `scripts/slo.json`, `scripts/lib/report.py`

- [ ] **Step 1:** אסוף telemetry מ־NiFi production אמיתי — התפלגות גדלי קבצים, קצב, יחס read/write, latency percentiles.
- [ ] **Step 2:** כתוב `scripts/slo.json` עם ערכי סף שמקורם במדידה, ולא בהערכה.
- [ ] **Step 3:** החזר ל־`report.py` פונקציית `verdict(agg, slo)` שמחזירה PASS/FAIL בלבד — בלי אותיות ובלי אמוג'י — עם ציטוט של הסף שנחצה.
- [ ] **Step 4:** החזר `diagnose()` רק עבור מסקנות שנתמכות בטלמטריה שנאספה: `retrans_pct` מ־Task 15 תומך ב"network retry"; `cpu_usr` תומך ב"client CPU"; שום דבר ב־fio אינו תומך ב"NFS lock contention" או ב"server GC", ולכן אלה לא חוזרים.
- [ ] **Step 5:** Commit: `git commit -m "feat: verdicts against measured SLOs, diagnoses only where telemetry supports them"`

## Task 22: NiFi cluster mode אמיתי

**Files:** `nifi/nifi-cluster.sh`

- [ ] **Step 1:** פרוס NiFi כ־cluster אמיתי עם ZooKeeper או embedded cluster coordinator.
- [ ] **Step 2:** הוסף load-balanced connections ו־primary-node processors ל־flow.
- [ ] **Step 3:** מדוד state replication ו־failover של ה־coordinator.
- [ ] **Step 4:** שמור את המצב הקיים כ־`standalone storage mode` — הוא תקף למה שהוא בודק, ולא יותר.
- [ ] **Step 5:** Commit: `git commit -m "feat: real NiFi cluster mode alongside standalone storage mode"`

---

## מה נשאר כמות שהוא

- מבנה הפרויקט והפרדת `jobs/` מ־`scripts/`.
- `direct=1` בבדיקות תקרת אחסון — עם התיעוד שזה אינו מסלול ה־I/O של NiFi.
- `group_reporting=1` לסיכום per-pod, לצד ה־time-series.
- ערכי ה־rate של `test12` — החישוב `(875+375)×8=10K` תקין.
- הפרופילים `smallfile` / `bigfile` / `churn` כנקודת פתיחה.
- ה־smoke test של RWO/RWX ו־`dsync`.
- repositories נפרדים לכל מופע NiFi.
- מנגנון פתירת ה־properties לפי גרסה ב־`nififlow.py` — הוא נכון ושימושי.

## מה יורד

- מחיקת PVC לפי `app.kubernetes.io/name` בלבד (Task 3).
- `parse_results_v2.py` — parser שני (Task 9).
- ציוני A/S/F ו־`nfs_diagnosis` — עד Task 21.
- `cpus_allowed_policy=split` (Task 4).
- ממוצע P99 כ־P99 של קלאסטר (Task 9, Task 14).
- `rate` כקריטריון הצלחה — הוא תקרת offered load בלבד (Task 4, Task 18).
- repository occupancy כמדד throughput (Task 11).
- `sleep` כמנגנון תזמון (Tasks 5, 7, 16).

---

## תנאי קבלה

ריצה נחשבת תקפה רק אם **כל** התנאים מתקיימים. ה־parser אוכף את 1–6, `collect_results.sh` את 7–8, ו־`nififlow.py` את 9–11.

1. `pods_collected == pods_expected` ב־`collection.json`.
2. כל פוד סיים ב־exit code 0, `restartCount == 0`.
3. `error == 0` בכל job ב־JSON של fio.
4. לכל כיוון ב־`expected_directions` יש `io_bytes > 0` ו־`runtime > 0`.
5. לכל כיוון עם I/O יש percentiles — אין בלוק חסר או קטוע.
6. `elapsed_s >= runtime_s * 0.9`.
7. אין `ENOSPC`, mount error או `Evicted` ב־`events.txt`.
8. `manifest.json` קיים ומכיל commit, image, StorageClass ו־`start_epoch`.
9. `counter_failures == 0` לכל node של NiFi.
10. אין bulletins ברמת ERROR.
11. `counter_files` תואם לספירת הקבצים ב־export בטווח של 1%.
12. להשוואה: `config_hash` זהה בכל הצדדים, ולפחות 3 חזרות עם רווחי סמך שאינם חופפים.

## שורה תחתונה

הבעיה אינה שהמספרים לא מדויקים — היא שאין דרך להבחין בין מספר נכון למספר שקרי. `results/test4_10pods_50k_5050_32kb` מוכיח את זה: עשרה פודים מתו מ־ENOSPC והדוח יצא ירוק.

P0 מסיר את שרשרת הכשל השקטה הזו: preflight מונע את הגורם, `exit $FIO_RC` מסמן את הכשל, ו־parser עם סכמה קשיחה מסרב לדווח עליו. P1 הופך את התוצאות לניתנות להשוואה בין ריצות ובין מערכי אחסון. P2 הוא מה שנדרש כדי לקרוא לזה benchmark שמייצג NiFi — וזה עדיין תלוי בטלמטריה מסביבה אמיתית שאין לנו כרגע.
