# FIO Benchmark Helm Chart

הרצת בדיקות אחסון FIO על OpenShift/Kubernetes, עם דגש אחד: **כל מספר שהחבילה מדווחת חייב לייצג עבודה שבאמת קרתה.**

ריצה שנכשלה חלקית נפסלת ולא מדווחת עם ציון נמוך. זו לא החמרה למען ההחמרה — ראה [למה זה חשוב](#למה-זה-חשוב).

## תוכן

- [התחלה מהירה](#התחלה-מהירה)
- [למה זה חשוב](#למה-זה-חשוב)
- [שני כללים של fio שחייבים להכיר](#שני-כללים-של-fio-שחייבים-להכיר)
- [הרצה ידנית עם helm](#הרצה-ידנית-עם-helm)
- [תרחישים מול פרופילים](#תרחישים-מול-פרופילים)
- [verdict ו־SLO](#verdict-ו־slo)
- [פרמטרים](#פרמטרים)
- [פתרון תקלות](#פתרון-תקלות)

---

## התחלה מהירה

```bash
# 1. פריסה. מדפיס RUN_ID.
./scripts/deploy_test.sh test_example_phase1 fio-tests

# 2. איסוף (לפי RUN_ID, לא לפי שם הבדיקה)
./scripts/collect_results.sh <RUN_ID> fio-tests

# 3. ניתוח
python3 scripts/parse_results.py results/<RUN_ID>

# 4. ניקוי
./scripts/cleanup_test.sh <RUN_ID> fio-tests
```

התחל מ־`test_example_phase1` — היא קטנה (2 GiB לפוד) ומאמתת שהצינור עובד לפני שמזמינים 40 פודים ו־8 טרהבייט.

**דרישות מוקדמות:** Helm 3, `kubectl`/`oc` מחובר, Python 3.9+, ו־image עם fio 3.41.

לרשימת הבדיקות ומה כל אחת בודקת: **[jobs/tests/README.md](jobs/tests/README.md)**
למטריצת העומסים (פרופילים מבודדים): **[jobs/profiles/README.md](jobs/profiles/README.md)**
לפרטי הסקריפטים ומשתני הסביבה: **[scripts/README.md](scripts/README.md)**
לבדיקות NiFi: **[nifi/README.md](nifi/README.md)**

---

## למה זה חשוב

בתיקיית `results/` יש ריצה אמיתית, `test4_10pods_50k_5050_32kb`, שבה **כל עשרת הפודים נכשלו**:

```
fio: ENOSPC on laying out file, stopping
fio: pid=0, err=28/file:filesetup.c:241, func=write, error=No space left on device
```

חצי ה־write של עומס 50/50 מת לגמרי. הדוח שיצא מזה:

```
OVERALL | 0.4 | 0.0 | 1.6 | 0.0 | ⚠️  WARN
```

ארבעה פודים ✅ PASS, שישה ⚠️ WARN, **אפס FAIL, ואפס אזכור לשגיאה**.

שרשרת הכשל הייתה: PVC קטן פי 8 מהנדרש → fio נכשל חלקית → הקונטיינר יצא ב־0 בכל מקרה → ה־parser אתחל כל שדה חסר ל־`0.0` → כל אפס נמצא מתחת לכל סף אזהרה → ירוק.

היום, אותם לוגים:

```
RUN REJECTED
No metrics are reported for a run that did not complete as declared.

  FAIL  fio-benchmark-0: fio error 28 (ENOSPC - the PVC could not hold size x numjobs)
  FAIL  fio-benchmark-0: no I/O in expected direction 'write'
  ...
No summary was written. Fix the run; do not report these numbers.
```

וברמה מוקדמת יותר, הפוד בכלל לא היה מתחיל:

```
dataset: size=10G x 8 clones = 80 GiB
mount:   10 GiB free
FATAL: PVC is too small for this job.
  Set pvc.size to at least 96Gi and re-run.
```

**הסכנה ההפוכה קיימת גם היא.** `test3_10pods_50k_5050_4kb` הייתה מוגדרת ב־`rate_iops=625`, שנותן 10,000 IOPS לפוד — **חמישית** ממה שהשם מבטיח. היא לא צעקה כלום. כל מי שקרא "50k" בדוח קיבל בפועל 10k, וזה מסוכן יותר מ־ENOSPC כי אין שום סימן.

---

## שני כללים של fio שחייבים להכיר

### `size` הוא לכל clone

```ini
size=10G
numjobs=8      ->  80 GiB, לא 10
```

```bash
python3 scripts/fio_capacity.py jobs/tests/<test>.fio     # כמה באמת צריך
```

`pvc_sizes.conf` נוצר אוטומטית מהערכים האלה. אחרי שינוי `size` או `numjobs` הרץ `./scripts/gen_pvc_sizes.sh`.

### `rate` ו־`rate_iops` הם לכל clone ולכל כיוון

ערך יחיד מגביל **כל כיוון בנפרד** — על job מעורב הוא נותן **פי 2** ממה שכתבת. אומת מול fio 3.41:

```
rate_iops=100    על randrw 50/50  ->  read: IOPS=99  write: IOPS=99
rate_iops=150,50 על randrw 70/30  ->  read: IOPS=149 write: IOPS=49
rate_iops=100    על randwrite     ->  write: IOPS=99          (חד־כיווני: תקין)
```

```
per_clone = target_iops_per_pod / numjobs
rate_iops = <per_clone * rwmixread>,<per_clone * (1 - rwmixread)>
```

**היעד בשם הקובץ הוא לכל פוד.** `test1_10pods_30k_5050_4kb` = 30,000 IOPS לפוד = 300,000 בקלאסטר.

---

## הרצה ידנית עם helm

אפשר, אבל שים לב לשינוי אחד:

> **`namespace` הוסר מ־`values.yaml`.** אם השתמשת ב־`--set namespace=X` — הוא כבר לא קיים. ה־namespace נקבע **רק** מ־`helm -n`.
>
> זה היה באג אמיתי: ה־templates כתבו `metadata.namespace` מ־`.Values.namespace` (ברירת מחדל `default`) בעוד הסקריפט העביר את ה־namespace רק ל־`helm -n`. ה־release נרשם ב־namespace אחד וה־Pods נוצרו באחר, ולכן איסוף וניקוי לא מצאו אותם, ושתי ריצות ב־namespaces שונים התנגשו ב־`default`.

```bash
helm install my-test . \
  -n fio-tests --create-namespace \
  --set replicaCount=10 \
  --set namePrefix=my-test \
  --set pvc.size=96Gi \
  --set pvc.storageClassName=sc-nas-nfs3 \
  --set-file fioJob.content=./jobs/tests/test4_10pods_50k_5050_32kb.fio
```

**גם בלי הסקריפט אתה מוגן:** שער הקיבולת רץ בתוך הפוד. אם ה־PVC קטן מדי הפוד יוצא ב־28 עם הסבר, לפני ש־fio נוגע בדיסק.

מה שמפסידים בהרצה ידנית: `run-id` (ולכן איסוף וניקוי מדויקים), התחלה מסונכרנת, `manifest.json`, ומחלקת המשאבים המתאימה לבדיקה. הסקריפט קיים בשביל אלה.

---

## תרחישים מול פרופילים

| | `jobs/tests/` | `jobs/profiles/` |
|---|---|---|
| שאלה | "האם המערך עומד בעומס הזה?" | "מה המערך עושה כשמבקשים ממנו את זה?" |
| דוגמה | `test4` — 10 פודים, 50K IOPS, 32k | `sync_write` — עלות durability |
| `direct=1` | תמיד | **לא** ב־`nifi_*` |

שניהם נפרסים באותה פקודה. ההבדל הקריטי: הפרופילים `nifi_content`, `nifi_flowfile`, `nifi_provenance` **לא משתמשים ב־`direct=1`**, כי NiFi כותב דרך ה־page cache ולא ב־`O_DIRECT`. בדיקה עם `O_DIRECT` מודדת מסלול I/O אחר, ולכן המספרים שלה **לא ניתנים להעברה ל־NiFi** — וזה נכון לגבי כל בדיקות התקרה ב־`jobs/tests/`.

## verdict ו־SLO

הדוח מציג `VERDICT: none` עד ש־[scripts/slo.json](scripts/slo.json) מכויל:

```json
{ "calibrated": true, "source": "prod-nifi telemetry, 2026-09", "targets": { ... } }
```

`parse_results.py` **מסרב** לקובץ שאינו מסומן `calibrated`, ומסרב למכויל שאין לו `source`. זו התנהגות מכוונת: PASS מול סף שהומצא גרוע מאין־verdict, כי הוא נראה כאילו הוא אומר משהו.

אבחנות מוצגות רק כשיש להן ראיה — CPU של הלקוח, throttling של cgroup, ו־retransmits מ־`mountstats`. הגרסה הישנה טענה "NFS lock contention" ו־"server GC" מתוך latency של fio בלבד; אף אחת מהן לא ניתנת לביסוס כך, ולכן הן נמחקו ולא רוככו.

## פרמטרים

| פרמטר | ברירת מחדל | הערה |
|---|---|---|
| `replicaCount` | `3` | מספר פודים |
| `image.repository` / `image.tag` | `rafmoshe2500/fio` / `3.41` | |
| `namePrefix` | `fio-benchmark` | חייב להיות DNS-1123: אותיות קטנות, ספרות, מקפים |
| `pvc.size` | `10Gi` | **חייב להיות ≥ `size × numjobs`** |
| `pvc.storageClassName` | `""` | |
| `mountPath` | `/mnt/fio-data` | |
| `resources.*` | `4` CPU / `4Gi` | `requests == limits` = Guaranteed QoS |
| `runId` | `""` | תווית `fio.benchmark/run-id` על כל משאב |
| `startEpoch` | `""` | epoch מוחלט להתחלה מסונכרנת. ריק = מיד |
| `prepare.enabled` | `false` | initContainer שמכין dataset. חובה לבדיקות read |
| `fioOutput.json` | `true` | `--output-format=json+` בין markers |
| `fioOutput.timeSeries` | `true` | לוגים של IOPS/BW/latency לשנייה |
| `podAntiAffinity.enabled` | `true` | פודים על workers שונים |

> `namespace` **אינו** פרמטר יותר. השתמש ב־`helm -n`.

---

## פתרון תקלות

### פוד יצא ב־28

שער הקיבולת עצר אותו. הלוג אומר בדיוק כמה צריך:

```
dataset: size=10G x 8 clones = 80 GiB
mount:   10 GiB free
FATAL: PVC is too small... Set pvc.size to at least 96Gi
```

### ה־parser פסל ריצה תקינה לכאורה

הוא בודק מול `jobs/tests/<test_id>.meta.json`. אם ערכת `.fio` בלי לעדכן את ה־meta — הם לא מסונכרנים. ראה [טבלת ההתאמה](jobs/tests/README.md#מה-שינוי-ב-fio-מחייב).

### "CPU cgroup throttled for Ns during the run"

הלקוח היה החסם, לא האחסון. ה־tail latency שנמדד הוא של מתזמן ה־CFS. הרץ מחדש עם מחלקת משאבים גבוהה יותר — `resource_class_for()` ב־[scripts/lib/common.sh](scripts/lib/common.sh).

### "no ===FIO_JSON_BEGIN=== marker"

הפוד נאסף לפני שסיים, או רץ עם `fioOutput.json=false`. `collect_results.sh` ממתין למצב סופי, אז זה בדרך כלל אומר שהאיסוף רץ עם timeout קצר מדי (`COLLECT_TIMEOUT`).

### פודים תקועים ב־Pending

```bash
kubectl get pvc -n fio-tests
kubectl describe pod <pod> -n fio-tests | sed -n '/Events:/,$p'
```

בדיקות גדולות מבקשות 4–8 CPU ו־96–384 GiB לפוד. אם ה־cluster קטן מדי, זה יראה כאן.

### התחלה לא מסונכרנת

הלוג יגיד:

```
barrier: WARNING started 45s late; overlap is not guaranteed
```

העלה את `BARRIER_LEAD` (ברירת מחדל 180 שניות) כדי לתת יותר זמן לקשירת PVC ומשיכת image.

---

## מבנה

```
├── templates/          Helm chart: pods, pvc, configmap
├── jobs/tests/         24 בדיקות: <id>.fio + <id>.meta.json  → README
├── scripts/            deploy / collect / parse / cleanup     → README
├── jobs/profiles/      13 פרופילים: מאפיין I/O אחד כל אחד     → README
├── nifi/               בדיקות עומס NiFi + chaos + cluster      → README
├── results/            תוצאות לפי RUN_ID
└── docs/               תוכנית התיקון המלאה
```

## רישיון

MIT
