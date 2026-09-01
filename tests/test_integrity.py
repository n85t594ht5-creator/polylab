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
ck("вердикт отражает неполноту", r2["verdict"] in ("RAW_INCOMPLETE", "RAW_SHRUNK"), r2["verdict"])
ck("причина названа человеку", any("потеряно" in i for i in r2["issues"]), str(r2["issues"][:1]))

print("\nraw отсутствует полностью")
os.remove(f"data/raw/dna/{DAY}.csv.gz")
r3 = IG.check([DAY])
ck("статус RAW_MISSING", r3["days"][DAY]["status"] == "RAW_MISSING")
ck("это тоже потеря", r3["verdict"] in ("RAW_INCOMPLETE", "RAW_SHRUNK"), r3["verdict"])

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

print("\nрегрессия PHASE 10: прошлое не блокирует, настоящее защищено")
# ROOT CAUSE: download-artifact видит только свой прогон → каждый прогон начинал
# суточный файл заново. Прошлые потери непоправимы, текущие — предотвратимы.
r_past = IG.check([DAY], today="2026-09-02")
ck("вчерашняя потеря → LEGACY_INCOMPLETE", r_past["days"][DAY]["status"] == "LEGACY_INCOMPLETE",
   r_past["days"][DAY]["status"])
ck("наследие не блокирует работу", r_past["verdict"] == "OK", r_past["verdict"])
ck("но зафиксировано отдельно", r_past["legacy_verdict"] == "LEGACY_INCOMPLETE")
ck("объяснено, почему не чинится", "не подлежит" in (r_past["days"][DAY].get("note") or ""))
r_now = IG.check([DAY], today=DAY)
ck("сегодняшняя потеря → RAW_INCOMPLETE", r_now["days"][DAY]["status"] == "RAW_INCOMPLETE",
   r_now["days"][DAY]["status"])
ck("сегодняшняя неполнота видна", r_now["verdict"] == "RAW_INCOMPLETE", r_now["verdict"])

print("\nинвариант непрерывности: raw не уменьшается между прогонами")
IG.save_baseline({DAY: {"raw_rows": 30, "at": "x"}})
r_sh = IG.check([DAY], today=DAY)
ck("уменьшение обнаружено", r_sh["days"][DAY].get("shrunk_by") == 22, str(r_sh["days"][DAY].get("shrunk_by")))
ck("вердикт RAW_SHRUNK", r_sh["verdict"] == "RAW_SHRUNK", r_sh["verdict"])
ck("прошлый размер записан", r_sh["days"][DAY]["previous_raw_rows"] == 30)
IG.save_baseline({DAY: {"raw_rows": 5, "at": "x"}})
r_gr = IG.check([DAY], today=DAY)
ck("рост не считается потерей", not r_gr["shrunk"], str(r_gr["shrunk"]))
ck("но неполнота видна информационно", r_gr["verdict"] == "RAW_INCOMPLETE", r_gr["verdict"])
IG.save_baseline({})

print("\nстрогий режим останавливает дозапись")
# подменяем «сегодня» на день данных, иначе они считались бы наследием
r4 = subprocess.run([sys.executable, "-c",
                     f"import sys;sys.path.insert(0,{ROOT!r});"
                     "from polylab.data import integrity as I;"
                     f"I.save_baseline({{{DAY!r}:{{'raw_rows':30,'at':'x'}}}});"
                     f"r=I.check([{DAY!r}],today={DAY!r});"
                     "print(r['verdict']);sys.exit(2 if r['shrunk'] else 0)"],
                    capture_output=True, text=True)
ck("падает до сбора при потере", r4.returncode == 2, f"код {r4.returncode}")
ck("вердикт RAW_SHRUNK при уменьшении", "RAW_SHRUNK" in r4.stdout, r4.stdout.strip()[-40:])
r5 = subprocess.run([sys.executable, os.path.join(ROOT, "polylab/data/integrity.py"), "after"],
                    capture_output=True, text=True)
ck("после сбора не падает, но сообщает",
   r5.returncode == 0 and ("НЕПОЛНОТА" in r5.stdout or "УМЕНЬШИЛСЯ" in r5.stdout),
   r5.stdout.strip()[-70:])

print("\nистория целостности накапливается")
rep = json.load(open("research/RAW_INTEGRITY.json"))
# в этом наборе main() вызывается один раз (второй вызов заменён прямым check)
ck("история ведётся", len(rep.get("history", [])) >= 1, str(len(rep.get("history", []))))
ck("в истории есть вердикт", all("verdict" in h for h in rep["history"]))
ck("в истории есть полнота", all("completeness" in h for h in rep["history"]))

bad = [n for n, o in R if not o]
print(f"\n{'='*52}\nINTEGRITY: {len(R)-len(bad)}/{len(R)}")
if bad: print("ПРОВАЛЕНО:", *bad, sep="\n  ")
sys.exit(1 if bad else 0)
