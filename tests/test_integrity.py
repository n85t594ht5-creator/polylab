"""Тесты PHASE 10: потеря raw видна отдельно от защиты агрегата."""
import os, sys, json, gzip, csv, tempfile, subprocess
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(tempfile.mkdtemp())

from polylab.data.store import Store
from polylab.data import integrity as IG

R = []
def ck(n, c, i=""):
    R.append((n, bool(c))); print(("  OK  " if c else " FAIL ") + n + ("  " + i if i else ""))

DAY = "2026-09-01"
dt = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
def row(sid):
    return {"snapshot_id": sid, "ts": dt.isoformat(), "asset": "BTC", "window": "5m",
            "current_price": 100.0, "reference_price": 99.0, "quality": "GOOD"}

def write_raw(n, seen_reset=True):
    s = Store()
    if seen_reset: s.seen = set()
    for i in range(n): s.add_dna(row(f"x@{i*15}"))
    s.flush_dna(dt)
    return s

def write_agg(n):
    os.makedirs("data/agg", exist_ok=True)
    json.dump({"day": DAY, "snapshots": n, "valid": n}, open(f"data/agg/{DAY}.json", "w"))

print("\nполный raw")
write_raw(30); write_agg(30)
r = IG.check([DAY])
ck("статус OK", r["days"][DAY]["status"] == "OK", r["days"][DAY]["status"])
ck("полнота 1.0", r["completeness"] == 1.0, str(r["completeness"]))
ck("вердикт OK", r["verdict"] == "OK")
ck("проблем нет", not r["issues"])

print("\nпотеря raw видна")
os.remove(f"data/raw/dna/{DAY}.csv.gz")
write_raw(8)                      # прогон начал файл заново
r2 = IG.check([DAY])
d = r2["days"][DAY]
ck("статус RAW_INCOMPLETE", d["status"] == "RAW_INCOMPLETE", d["status"])
ck("посчитано потерянное", d["lost"] == 22, str(d.get("lost")))
ck("полнота меньше 1", d["completeness"] < 1, str(d["completeness"]))
ck("вердикт RAW_LOSS", r2["verdict"] == "RAW_LOSS")
ck("причина названа человеку", any("потеряно" in i for i in r2["issues"]), str(r2["issues"][:1]))

print("\nraw отсутствует полностью")
os.remove(f"data/raw/dna/{DAY}.csv.gz")
r3 = IG.check([DAY])
ck("статус RAW_MISSING", r3["days"][DAY]["status"] == "RAW_MISSING")
ck("это тоже потеря", r3["verdict"] == "RAW_LOSS")

print("\nзащита агрегата не маскирует потерю")
write_raw(8)
out = subprocess.run([sys.executable, os.path.join(ROOT, "polylab/data/aggregate.py"), DAY],
                     capture_output=True, text=True)
a = json.load(open(f"data/agg/{DAY}.json"))
ck("агрегат не уменьшился", a["snapshots"] == 30, str(a["snapshots"]))
ck("флаг неполноты выставлен", a.get("raw_incomplete_on_rebuild") is True)
ck("видно, сколько реально в raw", a.get("raw_rows_seen") == 8, str(a.get("raw_rows_seen")))
ck("посчитана полнота", a.get("raw_completeness") is not None, str(a.get("raw_completeness")))
ck("предупреждение выведено", "raw неполон" in out.stdout, out.stdout.strip()[:60])
ck("это warning, а не тишина", "::warning::" in out.stdout)

print("\nстрогий режим останавливает дозапись")
r4 = subprocess.run([sys.executable, os.path.join(ROOT, "polylab/data/integrity.py"), "before"],
                    capture_output=True, text=True, env={**os.environ, "STRICT_RAW": "1"})
ck("падает до сбора при потере", r4.returncode == 2, f"код {r4.returncode}")
ck("объясняет причину", "закрепила бы потерю" in r4.stdout, r4.stdout.strip()[-60:])
r5 = subprocess.run([sys.executable, os.path.join(ROOT, "polylab/data/integrity.py"), "after"],
                    capture_output=True, text=True)
ck("после сбора не падает, но сообщает", r5.returncode == 0 and "ПОТЕРЯ" in r5.stdout)

print("\nистория целостности накапливается")
rep = json.load(open("research/RAW_INTEGRITY.json"))
ck("история ведётся", len(rep.get("history", [])) >= 2, str(len(rep.get("history", []))))
ck("в истории есть полнота", all("completeness" in h for h in rep["history"]))

bad = [n for n, o in R if not o]
print(f"\n{'='*52}\nINTEGRITY: {len(R)-len(bad)}/{len(R)}")
if bad: print("ПРОВАЛЕНО:", *bad, sep="\n  ")
sys.exit(1 if bad else 0)
