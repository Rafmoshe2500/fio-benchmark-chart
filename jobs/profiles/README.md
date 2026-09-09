# מטריצת עומסים

בעוד `jobs/tests/` מכיל **תרחישים** (10 פודים, 50K IOPS, 32k), התיקייה הזו מכילה **פרופילים**: כל אחד מבודד מאפיין I/O אחד. תרחיש עונה על "האם המערך עומד בעומס הזה"; פרופיל עונה על "מה המערך עושה כשמבקשים ממנו את זה".

שתי התיקיות נפרסות באותה צורה:

```bash
./scripts/deploy_test.sh nifi_flowfile fio-tests
```

---

## ⚠️ הפרופילים של NiFi לא משתמשים ב־`direct=1` — וזה הדבר החשוב כאן

`nifi_content`, `nifi_flowfile`, `nifi_provenance` ו־`smallfile_lifecycle` **אינם מגדירים `direct=1`**, בכוונה.

NiFi כותב את ה־repositories שלו דרך ה־page cache הרגיל. הוא לא פותח קבצים ב־`O_DIRECT`. כל בדיקה שכן משתמשת ב־`O_DIRECT` מודדת **מסלול I/O אחר** — ולכן המספרים שלה לא ניתנים להעברה ל־NiFi.

כל בדיקות התקרה ב־`jobs/tests/` משתמשות ב־`direct=1`. זה **נכון** לבדיקת תקרת אחסון ו**שגוי** לחיזוי התנהגות NiFi. עד עכשיו שום דבר לא אמר את זה.

| קבוצה | `direct` | מה זה מודד |
|---|---|---|
| `jobs/tests/*` | `1` | תקרת האחסון — עוקף cache של הלקוח |
| `nifi_*`, `smallfile_lifecycle` | לא מוגדר | המסלול ש־NiFi באמת עובר |
| שאר הפרופילים | `1` | מאפיין אחסון מבודד |

---

## הפרופילים

| פרופיל | מבודד | הערה חשובה |
|---|---|---|
| `low_qd_latency` | latency ללא עומס | `iodepth=1 numjobs=1`. **קו הבסיס** שכל מספר latency אחר צריך להיקרא מולו |
| `target_rate_steady` | עמידה ביעד לאורך זמן | `rate_iops_min` — fio עצמו נכשל אם הקצב לא נשמר. **דורש כיול** |
| `max_throughput` | תקרה | QD כולל 512. ה־latency שלו הוא של תור רווי — אסור להשוות למוגבלים |
| `cold_read` | קריאה ללא cache | "קר" **ללקוח בלבד** — ה־cache של השרת אינו בשליטתנו |
| `warm_read` | קריאה מ־cache | הזוג `cold`/`warm` תוחם את המערך |
| `sync_write` | עלות durability | `fsync=1` אחרי כל כתיבה — **לא** `end_fsync` |
| `smallfile_lifecycle` | metadata | create/stat/delete. המדד הוא RTT של LOOKUP/CREATE/REMOVE, לא bandwidth |
| `nifi_content` | content repository | claims של ~1MB שמתגלגלים ב־`max.appendable.size` |
| `nifi_flowfile` | flowfile repository | WAL עם `fsync=64` ≈ checkpoint מחזורי |
| `nifi_provenance` | provenance repository | journal → index → merge. מעורב כי ה־merge קורא מה שזה עתה נכתב |
| `overload_drain` | התנהגות מעבר לרוויה | המדד הוא צורת ה־latency וזמן הניקוז, לא throughput |
| `incompressible` | נתונים בלתי־דחיסים | אומת: יחס gzip **1.01x** |
| `compressible` | נתונים דחיסים | אומת: יחס gzip **1.98x** |

---

## שלושה זוגות שחייבים לרוץ יחד

מספר בודד מכל אחד מאלה חסר משמעות בלי בן הזוג שלו.

### `incompressible` מול `compressible`

הפער ביניהם על אותו StorageClass הוא **התועלת של dedup/compression במערך**. אימות:

```
incompressible  raw=67108864  gzip=66717925  ratio=1.01x
compressible    raw=67108864  gzip=33831043  ratio=1.98x
```

לצטט מספר בלי לומר מאיזה צד הוא בא = לצטט הנחה לא־מוצהרת על אופי הנתונים.

### `cold_read` מול `warm_read`

`cold` הוא מה שעולה החטאה ב־cache, `warm` הוא מה שעולה פגיעה. "latency של קריאה" בלי לומר איזה מהם — לא שמיש.

**מגבלה שצריך לרשום:** `direct=1` עוקף רק את ה־cache של **הלקוח**. ה־cache של שרת ה־NFS לא בשליטתנו ויכול בהחלט להחזיק את הנתונים. לקריאה קרה באמת צריך לרוקן את ה־cache של המערך או לעשות את הדאטהסט גדול ממנו — ולרשום מה נעשה.

### `low_qd_latency` מול כל השאר

`max_throughput` מדווח latency של **תור רווי** — הוא מודד את אורך התור, לא את המערך. בלי קו הבסיס של `low_qd_latency` אין דרך לדעת כמה מה־latency הוא queueing וכמה הוא service time.

---

## מה דורש כיול

`target_rate_steady` מכיל **placeholders**:

```ini
rate_iops=438,187
rate_iops_min=394,168
```

הם מסומנים `CALIBRATION REQUIRED` בקובץ. עד שיוחלפו בקצב שהסביבה באמת צריכה, הפרופיל בודק מספר שהומצא.

אותו דבר ל־`scripts/slo.json`: הוא template לא־מכויל, ו־`parse_results.py` **מסרב להשתמש בו** עד ש־`calibrated: true` ו־`source` מלאים. עד אז הדוח מציג נתונים ו־`VERDICT: none`.

זו התנהגות נכונה, לא חוסר. PASS מול סף שהומצא גרוע מאין־verdict, כי הוא **נראה** כאילו הוא אומר משהו.

**מה צריך למדוד כדי לכייל:**

1. התפלגות גדלי הקבצים ויחס read/write בזרימת NiFi האמיתית
2. הקצב שהסביבה צריכה — sustained ו־peak
3. ה־latency שבו האפליקציה מתחילה להידרדר (לא מספר עגול)
4. לרשום ב־`source` איך זה נמדד

---

## הרצה

```bash
# פרופיל בודד
./scripts/deploy_test.sh nifi_flowfile fio-tests

# זוג הדחיסות — אותו StorageClass, ריצות נפרדות
./scripts/deploy_test.sh incompressible fio-tests
./scripts/deploy_test.sh compressible fio-tests

# עם חזרות ורווח סמך (מומלץ לכל מספר שמצטטים)
REPEATS=3 ./scripts/run_repeated.sh low_qd_latency fio-tests
```

`cold_read` ו־`warm_read` מקבלים `allow_file_create=0` ו־`deploy_test.sh` מפעיל עבורם אוטומטית את ה־initContainer שמכין את הדאטהסט. בלעדיו fio היה קורא קבצים sparse ומודד אוויר.

## להוסיף פרופיל

אותם כללים כמו ב־[jobs/tests/README.md](../tests/README.md): בלוק `[global]` אחיד, `.meta.json` תואם, ואז `./scripts/gen_pvc_sizes.sh`.

ושאלה אחת נוספת שכדאי לשאול: **איזה מאפיין יחיד זה מבודד?** אם התשובה היא "כמה", זה תרחיש ומקומו ב־`jobs/tests/`.
