"""Тесты 5D: длинная серия, классификация задержек, 60m, пересборка."""
import os, sys, json, gzip, csv, tempfile, subprocess
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(tempfile.mkdtemp())
os.makedirs("research", exist_ok=True)

from polylab.data.store import Store
from polylab.data import aggregate as AGG
from polylab.data import validate as VAL

R = []
def ck(n, c, i=""):
    R.append((n, bool(c))); print(("  OK  " if c else " FAIL ") + n + ("  " + i if i else ""))

def row(sid, day, **kw):
    d = {"snapshot_id": sid, "ts": f"{day}T10:00:00+00:00", "asset": "BTC", "window": "5m",
         "current_price": 100.0, "reference_price": 99.0, "move": 0.0011, "elapsed": 0.8,
         "up_ask": 0.55, "down_ask": 0.44, "quality": "GOOD", "missing": "",
         "volatility": 0.001, "acceleration": 1.2, "market_id": "m1"}
    d.update(kw); return d

# ── длинная серия: 5 дней ──
print("\nдлинная серия, 5 дней")
days = [f"2026-09-0{i}" for i in range(1, 6)]
for di, day in enumerate(days):
    dt = datetime.fromisoformat(day + "T12:00:00+00:00")
    s = Store(); s.load_seen()
    for i in range(20):
        a = ["BTC", "ETH", "SOL", "XRP"][i % 4]
        w = "5m" if i % 3 else "15m"
        q = ["GOOD", "DEGRADED", "INCOMPLETE"][i % 3]
        s.add_dna(row(f"{a}{di}@{i*15}", day, asset=a, window=w, quality=q, market_id=f"mk{di}_{i%5}",
                      volatility=None if q == "DEGRADED" else 0.001,
                      missing="volatility" if q == "DEGRADED" else ""))
    s.flush_dna(dt)
    subprocess.run([sys.executable, os.path.join(ROOT, "polylab/data/aggregate.py"), day],
                   capture_output=True, text=True)

rep = VAL.validate(source="SYNTHETIC")
ck("все дни увидены", rep["days"] == 5, str(rep["days"]))
ck("снимки просуммированы", rep["totals"]["snapshots"] == 100, str(rep["totals"]["snapshots"]))
ck("источник помечен SYNTHETIC", rep["source"] == "SYNTHETIC")
ck("дублей id нет", rep["totals"]["duplicate_ids"] == 0, str(rep["totals"]["duplicate_ids"]))
ck("активы разложены", set(rep["totals"]["assets"]) == {"BTC", "ETH", "SOL", "XRP"})
ck("окна разложены", set(rep["totals"]["windows"]) == {"5m", "15m"}, str(rep["totals"]["windows"]))
ck("уникальные рынки посчитаны", rep["totals"]["unique_markets"] > 0, str(rep["totals"]["unique_markets"]))
ck("60m помечено как не наблюдавшееся",
   any(i["kind"] == "WINDOW_NOT_OBSERVED" and i["detail"] == "60m" for i in rep["issues"]))
ck("это НЕ утверждение об отсутствии",
   any("не утверждение" in (i.get("note") or "") for i in rep["issues"] if i["kind"] == "WINDOW_NOT_OBSERVED"))
ck("вердикт OK при чистых данных", rep["verdict"] == "OK", rep["verdict"])
ck("доступность признаков — доля, не ноль",
   0 < rep["totals"]["feature_availability_share"]["volatility"] < 1,
   str(rep["totals"]["feature_availability_share"]["volatility"]))

# ── обнаружение дублей ──
print("\nобнаружение дублей и расхождений")
dt = datetime.fromisoformat(days[0] + "T12:00:00+00:00")
s2 = Store(); s2.seen = set()
s2.add_dna(row("BTC0@0", days[0]))       # такой id уже есть в этом дне
s2.flush_dna(dt)
rep2 = VAL.validate(source="SYNTHETIC")
ck("дубль внутри дня найден",
   any(i["kind"] == "DUPLICATE_IDS" for i in rep2["issues"]),
   str([i["kind"] for i in rep2["issues"]][:4]))
ck("вердикт стал ISSUES", rep2["verdict"] == "ISSUES", rep2["verdict"])

# ── raw ↔ aggregate ──
print("\nсогласованность raw и агрегата")
bad_day = days[1]
a = json.load(open(f"data/agg/{bad_day}.json"))
a["snapshots"] = 999
json.dump(a, open(f"data/agg/{bad_day}.json", "w"))
rep3 = VAL.validate(source="SYNTHETIC")
ck("расхождение raw/агрегат найдено",
   any(i["kind"] == "RAW_AGG_MISMATCH" and i["day"] == bad_day for i in rep3["issues"]))
ck("несогласованность качества найдена",
   any(i["kind"] in ("QUALITY_SUM_MISMATCH", "VALID_MISMATCH") for i in rep3["issues"]))

# ── пересборка ──
print("\nпересборка агрегата")
subprocess.run([sys.executable, os.path.join(ROOT, "polylab/data/aggregate.py"), days[2]],
               capture_output=True, text=True)
rc = VAL.rebuild_check(days[2])
ck("пересборка детерминирована", rc["status"] == "DETERMINISTIC", str(rc))
os.remove(f"data/raw/dna/{days[3]}.csv.gz")
rc2 = VAL.rebuild_check(days[3])
ck("отсутствие локального raw — не ошибка", rc2["status"] == "RAW_NOT_LOCAL", str(rc2["status"]))
ck("сохранённый агрегат назван источником истины", "источник истины" in rc2.get("note", ""))

# ── классификация задержек ──
print("\nклассификация измеримости задержки")
from polylab.data import latency_probe as LP
ck("парсинг времени с мс", LP.parse_ts("2026-08-29T10:00:00.123456Z") is not None)
ck("битое время → None", LP.parse_ts("не время") is None)
ck("пустое время → None", LP.parse_ts("") is None)
ck("зонд пишет отдельно от Market DNA", LP.OUT.startswith("data/raw/latency_probe"), LP.OUT)
ck("в полях зонда есть флаг измеримости", "measurable" in LP.FIELDS)
ck("в полях есть предупреждение о часах", "clock_note" in LP.FIELDS)

# ── отчёт качества ──
print("\nотчёт качества")
json.dump(rep, open("research/LONG_SERIES.json", "w"))
json.dump({"windows_ever_seen": {"5m": 10, "15m": 4}, "total_observations": 3,
           "note_60m": "не наблюдалось; НЕ утверждение об отсутствии"},
          open("research/WINDOW_OBSERVATION.json", "w"))
json.dump({"status": "OK", "samples": 200, "interval_target_ms": 500,
           "measurable_counts": {"NOT_MEASURABLE": 200},
           "request_latency_ms": {"median": 90, "p90": 120, "p95": 140, "p99": 200, "max": 250},
           "book_changes": 40, "book_change_rate": 0.2, "min_measurable_latency_ms": 620,
           "verdict": "INCONCLUSIVE", "limits": ["короче интервала опроса неизмеримо"]},
          open("research/LATENCY_PROBE.json", "w"))
sys.path.insert(0, ROOT)
from polylab.data import quality_report as QR
data, md = QR.build()
st = {f["area"]: f["status"] for f in data["findings"]}
ck("задержка → INCONCLUSIVE", st["Измеримость задержки"] == "INCONCLUSIVE", st["Измеримость задержки"])
ck("задержка запроса → CONFIRMED", st["Задержка запроса"] == "CONFIRMED")
ck("60m → NOT OBSERVED", st["60-минутные окна"] == "NOT OBSERVED")
ck("объём → UNAVAILABLE", st["Объём торгов Polymarket"] == "UNAVAILABLE")
ck("малая выборка не даёт CONFIRMED", st["Целостность длинной серии"] in ("SUPPORTED", "INCONCLUSIVE"),
   st["Целостность длинной серии"])
# Проверяем смысл, а не подстроку: дисклеймер «это НЕ утверждение об отсутствии»
# сам содержит слова «не существует», и это правильно.
ASSERTIONS = ["60m markets do not exist", "60m отсутствуют", "60-минутных окон не существует",
              "60m не существует", "рынков 60m нет"]
ck("отчёт не утверждает отсутствие 60m", not any(p in md for p in ASSERTIONS),
   str([p for p in ASSERTIONS if p in md]))
ck("в отчёте есть статус NOT OBSERVED", "NOT OBSERVED" in md)
ck("есть дисклеймер о том, что это не отсутствие",
   "не наблюд" in md.lower() and ("не утверждение" in md or "выводов об их отсутствии не делать" in md))
# формулировка менялась — проверяем смысл: отчёт обязан отговаривать от
# торговых выводов по задержке и называть её неизмеримой/недоказанной
ck("рекомендация про неизмеримость есть",
   ("не измеряется" in md or "неизмерим" in md.lower()) and
   ("не делаем" in md or "нельзя" in md), md[md.find("## Рекомендация"):][:200].replace("\n"," ")[:120])
ck("markdown собран", md.startswith("# POLYLAB") and "| Направление |" in md)

bad = [n for n, o in R if not o]
print(f"\n{'='*52}\nPHASE-5D: {len(R)-len(bad)}/{len(R)}")
if bad: print("ПРОВАЛЕНО:", *bad, sep="\n  ")
sys.exit(1 if bad else 0)
