# RUNBOOK — איך מריצים

מדריך תפעולי. אם קראת רק קובץ אחד — שיהיה זה.

---

## TL;DR — המטרה שלך

**"להריץ את אותן בדיקות על סביבות שונות ולהשוות."** שלוש פקודות:

```bash
REPEATS=3 ./scripts/run_suite.sh characterise nfs3
REPEATS=3 ./scripts/run_suite.sh characterise nfs41
python3 scripts/compare_envs.py results/suites/characterise-nfs3-* results/suites/characterise-nfs41-*
```

זהו. השאר במסמך הוא פירוט.

---

## הגדרה חד־פעמית

ערוך את [scripts/environments.json](scripts/environments.json) — הוסף כל מערך/StorageClass שתרצה להשוות:

```json
{
  "environments": {
    "nfs3":  { "storage_class": "sc-nas-nfs3",  "description": "NFSv3" },
    "nfs41": { "storage_class": "sc-nas-nfs41", "description": "NFSv4.1" },
    "vast":  { "storage_class": "vast-nfs",     "description": "VAST" }
  }
}
```

זה כל מה שצריך. כל suite ירוץ מול כל סביבה לפי שם.

---

## שכבה 1 — הריצה שאתה באמת צריך

### `run_suite.sh` — סט בדיקות מול סביבה אחת

```bash
./scripts/run_suite.sh <suite> <environment> [namespace]
```

```bash
# בדיקת שפיות ראשונה — קטן ומהיר
./scripts/run_suite.sh smoke nfs3

# האמיתי, עם חזרות
REPEATS=3 ./scripts/run_suite.sh characterise nfs3
```

מריץ כל בדיקה בסט: פריסה → איסוף → ניתוח → ניקוי → המתנה → הבאה. בדיקות **לא רצות במקביל** ולכן לא מתחרות זו בזו.

| suite | בדיקות | למה |
|---|---|---|
| `smoke` | 1 | לאמת שהצינור עובד |
| `quick` | 3 | סבב מהיר |
| `characterise` | 8 | **אפיון מלא של מערך** — הכי שימושי להשוואה |
| `nifi` | 4 | פרופילים שמדמים NiFi |
| `scenarios` | 6 | תרחישי העומס המקוריים |
| `ceiling` | 3 | תקרות |

לראות מה יש: `./scripts/run_suite.sh` בלי ארגומנטים.

| משתנה | ברירת מחדל | |
|---|---|---|
| `REPEATS` | `1` | **השתמש ב־3+ לכל מספר שתצטט** |
| `SETTLE` | `60` | שניות בין ריצות, שהמערך יירגע |

### `compare_envs.py` — ההשוואה

```bash
python3 scripts/compare_envs.py results/suites/<baseline> results/suites/<other> [...]
```

הראשון הוא הבסיס. הפלט:

```
  test4_10pods_50k_5050_32kb
  * IOPS      nfs3=48,000.0 nfs41=61,000.0    (nfs41 +27.1%)
      nfs41 higher

  test1_10pods_30k_5050_4kb
    IOPS      nfs3=29,800.0 nfs41=30,200.0    (nfs41 +1.3%)
      no difference established -- intervals overlap
```

**`*` = הפרש אמיתי.** בלי כוכבית — הפער קטן מהרעש, וזה **לא** ממצא, גם אם המדיאנים נראים שונים.

הכלי **מסרב** להשוות suites שונים, ומזהיר על commit שונה, working tree מלוכלך, פחות מ־2 חזרות, ובדיקה שחסרה בסביבה אחת.

---

## שכבה 2 — בדיקה בודדת

כשאתה חוקר משהו ספציפי:

```bash
STORAGE_CLASS=sc-nas-nfs3 ./scripts/deploy_test.sh test4_10pods_50k_5050_32kb fio-tests
```

מדפיס `run=<RUN_ID>`. משם:

```bash
./scripts/collect_results.sh <RUN_ID> fio-tests
python3 scripts/parse_results.py results/<RUN_ID>
./scripts/cleanup_test.sh <RUN_ID> fio-tests
```

> **השינוי היחיד מהגרסה הישנה:** איסוף וניקוי מקבלים **`RUN_ID`**, לא `test_id`. בלי זה אי אפשר להפריד בין שתי ריצות של אותה בדיקה.
>
> שכחת? הוא ב־`results/.last_run_id`, או הרץ `./scripts/cleanup_test.sh` בלי ארגומנטים כדי לראות מה קיים.

### בדיקה אחת עם חזרות

```bash
REPEATS=3 ./scripts/run_repeated.sh test4_10pods_50k_5050_32kb fio-tests
```

מדפיס מדיאן ורווח סמך של 95%.

---

## שכבה 3 — NiFi

```bash
# lifecycle מלא: פריסה, barrier, warm-up, איפוס מונים, מדידה, drain, אימות
STORAGE_CLASS=sc-nas-nfs3 ./nifi/nifi-nfs-loadtest.sh run 1800

# השוואת שתי סביבות במקביל (הגדרה ב-nifi/deployments.json)
./nifi/nifi-multi.sh run 1800

# הזרקת תקלות
./nifi/chaos.sh campaign

# cluster אמיתי (נפרד!)
STORAGE_CLASS=sc-nas-nfs3 ./nifi/nifi-cluster.sh deploy
./nifi/nifi-cluster.sh failover
```

> `run` הוא מה שרוצים. `record` לבדו דוגם את מה שקורה במקרה — טוב לצפייה, לא לדיווח.

---

## כל הסקריפטים

| סקריפט | מתי | שכבה |
|---|---|---|
| **`run_suite.sh`** | **סט בדיקות מול סביבה** | 1 |
| **`compare_envs.py`** | **השוואה בין סביבות** | 1 |
| `deploy_test.sh` | בדיקה בודדת | 2 |
| `collect_results.sh` | איסוף (לפי RUN_ID) | 2 |
| `parse_results.py` | ניתוח / פסילה | 2 |
| `cleanup_test.sh` | ניקוי (לפי RUN_ID) | 2 |
| `run_repeated.sh` | בדיקה אחת × N | 2 |
| `deploy_scale_steps.sh` | עקומת scaling (אוטומטי מ־`*gradual_scale*`) | — |
| `fio_capacity.py` | כמה מקום בדיקה צריכה | עזר |
| `gen_pvc_sizes.sh` | **הרץ אחרי שינוי `size`/`numjobs`** | עזר |
| `gen_tests_table.sh` | מרענן טבלה ב־README | עזר |
| `nifi/nifi-nfs-loadtest.sh` | עומס NiFi standalone | 3 |
| `nifi/nifi-multi.sh` | כמה סביבות NiFi במקביל | 3 |
| `nifi/chaos.sh` | תקלות ו־recovery | 3 |
| `nifi/nifi-cluster.sh` | NiFi cluster אמיתי | 3 |
| `nifi/nfsstat.py` | latency של NFS RPC | עזר |

---

## מה קורה כשמשהו נכשל — וזה בכוונה

הכלי **מסרב לדווח** על ריצה פגומה במקום לתת לה ציון נמוך.

| מה תראה | מה זה | מה לעשות |
|---|---|---|
| `exit 28` + `FATAL: PVC is too small` | שער הקיבולת עצר לפני fio | הלוג אומר את הגודל הנדרש |
| `RUN REJECTED` | ריצה לא הושלמה כפי שהוצהר | תקן; אל תצטט את המספרים |
| `CPU cgroup throttled` | **הלקוח** היה החסם, לא האחסון | הרץ שוב עם מחלקת משאבים גבוהה |
| `VERDICT: none` | אין SLO מכויל | תקין. ראה למטה |
| `no difference established` | הפער קטן מהרעש | הוסף חזרות, או שאין הפרש |
| `NOT COMPARABLE` (NiFi) | ההגדרות שונות | שנה משתנה אחד בלבד |

---

## מה עדיין דורש כיול

`scripts/slo.json` הוא template **לא־מכויל**, ולכן הדוח מציג `VERDICT: none`.

זה **מכוון**. PASS מול סף שהומצא גרוע מאין־verdict, כי הוא נראה כאילו הוא אומר משהו.

כדי לכייל, מדוד בסביבת NiFi האמיתית: התפלגות גדלי קבצים, יחס read/write, הקצב הנדרש, וה־latency שבו האפליקציה מידרדרת. ואז:

```json
{ "calibrated": true, "source": "prod-nifi telemetry 2026-09",
  "targets": { "test4_10pods_50k_5050_32kb": { "min_iops": 400000, "max_p99_ms": 20.0 } } }
```

אותו דבר ל־`rate_iops` ב־`jobs/profiles/target_rate_steady.fio`.

---

## שלושה כללים שימנעו ממך את הטעויות שכבר קרו

1. **`size` הוא לכל clone.** `size=10G numjobs=8` = 80 GiB. זה מה שהרג את `test4`.
2. **`rate_iops` הוא לכל clone ולכל כיוון.** ערך יחיד על job מעורב = **פי 2**.
3. **ריצה בודדת אינה מדידה.** תמיד `REPEATS=3` למספר שמצטטים.

---

## סדר מומלץ לסביבה חדשה

```bash
# 1. שפיות (~5 דק')
./scripts/run_suite.sh smoke <env>

# 2. אפיון (~2-3 שעות עם 3 חזרות)
REPEATS=3 ./scripts/run_suite.sh characterise <env>

# 3. אותו דבר על הסביבה השנייה
REPEATS=3 ./scripts/run_suite.sh characterise <env2>

# 4. השוואה
python3 scripts/compare_envs.py results/suites/characterise-<env>-* results/suites/characterise-<env2>-*

# 5. אם NiFi רלוונטי
REPEATS=3 ./scripts/run_suite.sh nifi <env>
STORAGE_CLASS=<sc> ./nifi/nifi-nfs-loadtest.sh run 1800
```

**עצור אחרי שלב 1** אם משהו נכשל. בדיקה של 2 GiB שנכשלת חוסכת ממך גילוי אותה תקלה על 8 טרהבייט.
