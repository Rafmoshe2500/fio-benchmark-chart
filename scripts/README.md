# סקריפטים — הרצה, איסוף וניתוח

## הזרימה המלאה

```bash
./scripts/deploy_test.sh  test4_10pods_50k_5050_32kb fio-tests
./scripts/collect_results.sh <RUN_ID> fio-tests
python3 scripts/parse_results.py results/<RUN_ID>
./scripts/cleanup_test.sh <RUN_ID> fio-tests
```

`deploy_test.sh` מדפיס את ה־`RUN_ID` ושומר אותו ב־`results/.last_run_id`.

## מה השתנה מהגרסה הקודמת

| | קודם | עכשיו |
|---|---|---|
| פריסה | `deploy_test.sh <test_id> [ns]` | **זהה** |
| איסוף | `collect_results.sh <test_id> [ns]` | `collect_results.sh <RUN_ID> [ns]` |
| ניקוי | `cleanup_test.sh <test_id> [ns]` | `cleanup_test.sh <RUN_ID> [ns]` |
| ניתוח | `parse_results.py <dir>` | **זהה** (אבל עכשיו יכול לפסול) |

**השינוי היחיד בשימוש:** איסוף וניקוי מקבלים `RUN_ID`, לא `test_id`. בלי זה אי אפשר להפריד בין שתי ריצות של אותה בדיקה, ואי אפשר לנקות ריצה אחת בלי לפגוע בשנייה.

שכחת את ה־RUN_ID? הוא ב־`results/.last_run_id`, ב־`results/<RUN_ID>/manifest.json`, או:

```bash
./scripts/cleanup_test.sh          # מדפיס את הריצות שקיימות ב־namespace
```

## מזהה הריצה

כל פריסה מייצרת `RUN_ID` בצורה `<safe-test-id>-<YYYYmmdd-HHMMSS>`, והוא מוטבע כתווית `fio.benchmark/run-id` על כל Pod, PVC ו־ConfigMap.

זה מה שמאפשר לאיסוף ולניקוי להיות מדויקים. קודם האיסוף עשה `kubectl get pods | grep "fio-"` — כלומר תפס גם ריצות ישנות וגם ריצות מקבילות — והניקוי מחק כל PVC עם `app.kubernetes.io/name=fio-benchmark` ב־namespace, כולל דאטהסטים שהוכנו לבדיקות read.

## הקבצים

| קובץ | תפקיד |
|---|---|
| `deploy_test.sh` | פריסה: preflight, מחלקת משאבים, barrier, manifest |
| `deploy_scale_steps.sh` | עקומת scaling — מדרגה שלמה בכל פעם |
| `collect_results.sh` | איסוף scoped, המתנה למצב סופי, בדיקת exit code ו־restarts |
| `cleanup_test.sh` | ניקוי לפי ה־release שנרשם, עם preview לפני מחיקה |
| `parse_results.py` | ניתוח — או פסילה |
| `fio_capacity.py` | כמה מקום הבדיקה באמת צריכה |
| `gen_pvc_sizes.sh` | מייצר את `pvc_sizes.conf` מקובצי ה־fio |
| `gen_tests_table.sh` | מייצר את הטבלה ב־`jobs/tests/README.md` |
| `pvc_sizes.conf` | גודל PVC לכל בדיקה — **נוצר אוטומטית, לא לערוך ביד** |
| `lib/` | `common.sh`, `fiojob.py`, `fiojson.py`, `testmeta.py`, `report.py` |
| `tests/` | 38 טסטים + fixtures |

## משתני סביבה

| משתנה | ברירת מחדל | מה זה עושה |
|---|---|---|
| `STORAGE_CLASS` | `sc-nas-nfs3` | ה־StorageClass לבדיקה |
| `BARRIER_LEAD` | `180` | שניות עד ההתחלה המסונכרנת. העלה אם ה־PVC נקשר לאט |
| `STEPS` | `10 20 40 80` | מדרגות ב־`deploy_scale_steps.sh` |
| `COLLECT_TIMEOUT` | `3600` | כמה לחכות שהפודים יסיימו |
| `FORCE` | `false` | `true` = ניקוי בלי לשאול |

## שלוש הגנות שמונעות תוצאה שקרית

### 1. preflight קיבולת

`size` ב־fio הוא **לכל clone**. `size=10G` עם `numjobs=8` דורש 80 GiB.

זה מה שהרג את `results/test4_10pods_50k_5050_32kb`: PVC של 10Gi, `ENOSPC on laying out file` בכל עשרת הפודים, חצי ה־write מת — והדוח יצא ⚠️ WARN עם ארבעה ✅ PASS.

עכשיו:

```
$ python3 scripts/fio_capacity.py jobs/tests/test4_10pods_50k_5050_32kb.fio --pvc 10Gi
PVC 10Gi is too small.
  size=10GiB x numjobs=8 = 80GiB of dataset
  with 20% headroom that is 96Gi
  fio will fail with ENOSPC partway through the run.
```

**אותה בדיקה רצה גם בתוך הפוד**, אחרי שה־PVC כבר mounted. כך שגם `helm install` ידני בלי הסקריפט לא יכול לשחזר את זה — הפוד יוצא ב־28 עם הסבר לפני ש־fio מתחיל.

### 2. `exit $FIO_RC`

הקונטיינר היה מסתיים ב־`echo`, ולכן החזיר 0 בכל מקרה — ו־`kubectl get pods` הראה `Completed` על ריצה שמתה. עכשיו קוד היציאה של fio עובר החוצה, ו־`collect_results.sh` מדווח עליו.

### 3. ה־parser פוסל

ריצה נפסלת אם:

- פוד כלשהו החזיר שגיאת fio
- כיוון שהוצהר ב־`expected_directions` לא ייצר I/O
- כיוון עם I/O חסר percentiles (לוג קטוע)
- מספר הפודים שונה מ־`replicas` המוצהר
- `elapsed` נמוך מ־90% מה־`runtime` המוצהר
- ה־cgroup מראה CPU throttling

הפסילה האחרונה חשובה במיוחד: אם הקונטיינר נחנק ב־CFS, ה־tail latency ש־fio מדד הוא של המתזמן ולא של האחסון, ואי אפשר להפריד ביניהם בדיעבד.

```
RUN REJECTED
No metrics are reported for a run that did not complete as declared.

  FAIL  fio-benchmark-0: fio error 28 (ENOSPC - the PVC could not hold size x numjobs)
  FAIL  fio-benchmark-0: no I/O in expected direction 'write'
  ...
No summary was written. Fix the run; do not report these numbers.
```

exit 2, ואין `summary_report.csv`.

## מחלקות משאבים

`requests == limits` בכל המחלקות — זה **Guaranteed QoS**, המחלקה היחידה שה־kubelet לא מצנן ראשונה.

| מחלקה | CPU / MEM | בדיקות |
|---|---|---|
| light | 1 / 1Gi | `test_example*` |
| normal | 4 / 4Gi | כל הבדיקות המוגבלות בקצב |
| heavy | 8 / 8Gi | `*1pod_max*`, `test10_burst*`, `test11_burst*` |

הבסיס למספרים: `test7` הגיע ל־~170K IOPS ו־665 MiB/s של 4K אקראי עם `refill_buffers=1` — כלומר שני שליש GiB לשנייה של נתונים בלתי־דחיסים שנוצרים מחדש. זו עבודת CPU, וב־2 ליבות הלקוח נשבר לפני המערך.

והחשוב: הפוד קורא `cgroup cpu.stat` לפני ואחרי, וה־parser פוסל אם היה throttling. זה ההבדל בין "העלינו CPU" לבין "אנחנו יודעים שהלקוח לא היה החסם".

## מה יוצא מריצה

```
results/<RUN_ID>/
├── manifest.json          מה נפרס: commit, storage class, releases, start epoch
├── collection.json        כמה פודים ציפינו מול כמה נאספו
├── fio-<prefix>-N.log     לוג מלא לכל פוד + json+ + time-series בין markers
├── pods.json  pvcs.json  nodes.json  events.txt
├── summary_report.csv     ← רק אם הריצה תקפה
└── summary_report.json    ← רק אם הריצה תקפה
```

ריצה מדורגת מוסיפה `steps.json` ותת־תיקייה `step-<n>/` לכל מדרגה.

## חזרות

ריצה בודדת אינה מדידה. הפרש של 8% בין שני StorageClasses לא אומר כלום בלי לדעת את הפיזור בתוך כל אחד. `lib/stats.py` מספק median ורווח סמך של 95%; ההמלצה היא 3 חזרות לפחות לפני שמצטטים מספר.

## פיתוח

```bash
cd scripts && python3 -m unittest discover -s tests -v
```

38 טסטים. `RealMetaFilesTest` טוען את כל 24 קובצי המטא־דאטה האמיתיים, כך שהוספת `.fio` בלי `.meta.json` תואם נכשלת אוטומטית.
