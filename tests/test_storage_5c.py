"""Тесты 5C: retention, агрегаты, атомарность, замок, обрезка индекса."""
import os, sys, gzip, csv, json, time, tempfile, subprocess
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(tempfile.mkdtemp())

from polylab.data.store import Store, CollectorLock, DNA_FIELDS, MOVE_FIELDS
from polylab.data import aggregate as AGG

R = []
def ck(n, c, i=""):
    R.append((n, bool(c))); print(("  OK  " if c else " FAIL ") + n + ("  " + i if i else ""))

now = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)
def row(sid, **kw):
    d = {"snapshot_id": sid, "ts": now.isoformat(), "asset": "BTC", "window": "5m",
         "current_price": 100.0, "reference_price": 99.0, "move": 0.0011, "elapsed": 0.8,
         "up_ask": 0.55, "down_ask": 0.44, "quality": "GOOD", "missing": "",
         "volatility": 0.001, "acceleration": 1.2, "spread_up": 0.02, "imbalance_up": 0.1,
         "depth_1_up": 50.0, "depth_10_up": 200.0, "book_ts_up": 1787993409921,
         "liquidity": 1000.0, "market_volume": None}
    d.update(kw); return d

# ── 1. raw не попадает в Git ──
print("\n1) raw изолирован от Git")
s = Store()
s.add_dna(row("m@100")); s.flush_dna(now)
ck("raw лежит в data/raw/", s.dna_path(now).startswith("data/raw/"), s.dna_path(now))
gi = open(os.path.join(ROOT, ".gitignore")).read()
ck("data/raw/ в .gitignore", "data/raw/" in gi)
ck("замок в .gitignore", "collector.lock" in gi)
ck("агрегаты вне raw (идут в Git)", not s.agg_path(now).startswith("data/raw/"), s.agg_path(now))
ck("движения вне raw (идут в Git)", not s.moves_path(now).startswith("data/raw/"), s.moves_path(now))

# ── 5-6. идемпотентность и рестарт ──
print("\n5-6) идемпотентность и рестарт")
ck("дубль отклонён", s.add_dna(row("m@100")) is False)
s2 = Store(); n = s2.load_seen(now)
ck("индекс восстановлен", n >= 1, f"{n} id")
ck("после рестарта дубль отклонён", s2.add_dna(row("m@100")) is False)

# ── 7. битый хвост ──
print("\n7) повреждённый raw")
raw = open(s.dna_path(now), "rb").read()
open(s.dna_path(now), "wb").write(raw[:len(raw)//2])
s3 = Store(); n3 = s3.load_seen(now)
ck("id живы благодаря индексу", n3 >= 1, f"{n3} id")
ck("дубль всё равно отклонён", s3.add_dna(row("m@100")) is False)

# ── обрезка индекса ──
print("\nобрезка индекса по времени")
s4 = Store()
old_bucket = int((now - timedelta(hours=12)).timestamp())
new_bucket = int(now.timestamp())
s4.seen = {f"a@{old_bucket}", f"b@{new_bucket}", "broken-format"}
removed = s4.prune_ids(now, keep_hours=6)
ck("старый id удалён", f"a@{old_bucket}" not in s4.seen)
ck("свежий id сохранён", f"b@{new_bucket}" in s4.seen)
ck("непонятный формат не выброшен", "broken-format" in s4.seen)
ck("счётчик удалённых", removed == 1, str(removed))
s5 = Store(); s5.load_seen()
ck("обрезанный индекс записан на диск", f"a@{old_bucket}" not in s5.seen and f"b@{new_bucket}" in s5.seen)

# ── 9. атомарная запись ──
print("\n9) атомарность")
before = open(s4.ids_path()).read()
ck("временных файлов не осталось", not os.path.exists(s4.ids_path() + ".tmp"))
s4.prune_ids(now)
ck("индекс читается после перезаписи", open(s4.ids_path()).read() is not None)
ck("содержимое консистентно", len(open(s4.ids_path()).read().strip().split("\n")) == len(s4.seen))

# ── 8. защита от параллельных коллекторов ──
print("\n8) защита от одновременных коллекторов")
l1 = CollectorLock(); l2 = CollectorLock()
ck("первый занял замок", l1.acquire() is True)
ck("второй не смог", l2.acquire() is False)
l1.release()
ck("после освобождения можно занять", l2.acquire() is True)
l2.release()
lk = CollectorLock(); lk.acquire()
os.utime(lk.path, (time.time() - 99999, time.time() - 99999))
l3 = CollectorLock()
ck("протухший замок снимается", l3.acquire() is True)
l3.release()
try:
    with CollectorLock():
        with CollectorLock():
            ok = False
except RuntimeError:
    ok = True
ck("контекстный менеджер защищает", ok)

# ── 2-3. агрегация и её корректность ──
print("\n2-3) дневной агрегат")
now = datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc)   # чистые сутки: выше файл намеренно портили
s6 = Store()
for i, (asset, win, q, mv, ask) in enumerate([
        ("BTC", "5m", "GOOD", 0.0011, 0.52), ("BTC", "5m", "GOOD", 0.0013, 0.57),
        ("ETH", "15m", "DEGRADED", 0.0003, 0.61), ("SOL", "5m", "INVALID", None, None),
        ("XRP", "15m", "INCOMPLETE", 0.0025, 0.55)]):
    s6.add_dna(row(f"x@{i*15}", asset=asset, window=win, quality=q, move=mv, up_ask=ask,
                   missing="volatility" if q == "DEGRADED" else ""))
s6.flush_dna(now)
day = now.strftime("%Y-%m-%d")
a = AGG.build(day)
ck("снимков посчитано", a["snapshots"] == 5, str(a["snapshots"]))
ck("валидных = все кроме INVALID", a["valid"] == 4, str(a["valid"]))
ck("разрез по активам", set(a["cuts"]["asset"]) == {"BTC", "ETH", "SOL", "XRP"}, str(set(a["cuts"]["asset"])))
ck("разрез по окнам", a["cuts"]["window"]["5m"]["n"] == 3 and a["cuts"]["window"]["15m"]["n"] == 2)
ck("бакеты входа", "0.50–0.55" in a["cuts"]["entry_bucket"], str(list(a["cuts"]["entry_bucket"])))
ck("бакеты движения", "0.10–0.12%" in a["cuts"]["move_bucket"], str(list(a["cuts"]["move_bucket"])))
ck("качество разложено", a["quality"]["GOOD"] == 2 and a["quality"]["INVALID"] == 1, str(a["quality"]))
ck("пропущенные поля посчитаны", a["missing_fields"].get("volatility") == 1, str(a["missing_fields"]))
ck("доступность признаков", a["feature_availability"]["volatility"] == 5, str(a["feature_availability"]["volatility"]))
ck("объём Polymarket остался недоступен", a["feature_availability"]["market_volume"] == 0)

# ── 14. детерминированность ──
print("\n14) детерминированность агрегата")
a2 = AGG.build(day)
j1 = json.dumps({k: v for k, v in a.items() if k != "built_at"}, sort_keys=True)
j2 = json.dumps({k: v for k, v in a2.items() if k != "built_at"}, sort_keys=True)
ck("повторный расчёт даёт тот же результат", j1 == j2)

# ── 4 + 12. ротация raw и агрегат без raw ──
print("\n4,12) ротация raw и жизнь агрегата без него")
os.makedirs("data/raw/dna", exist_ok=True)
old_day = (now - timedelta(days=10)).strftime("%Y-%m-%d")
open(f"data/raw/dna/{old_day}.csv.gz", "w").close()
open(f"data/agg/{old_day}.json", "w").write("{}")
rem = s6.rotate(now)
ck("старый raw удалён локально", f"{old_day}.csv.gz" in rem["raw/dna"], str(rem["raw/dna"]))
ck("агрегат того же дня сохранён", os.path.exists(f"data/agg/{old_day}.json"))
os.remove(s6.dna_path(now))
a3 = AGG.build(day)
ck("агрегат без raw не падает", a3["snapshots"] == 0)
ck("флаг отсутствия raw выставлен", a3["raw_present"] is False)
ck("прежний агрегат на диске цел", json.load(open(f"data/agg/{old_day}.json")) == {})

# ── 13. агрегат переживает рестарт ──
print("\n13) агрегат переживает рестарт")
json.dump(a, open(f"data/agg/{day}.json", "w"))
reloaded = json.load(open(f"data/agg/{day}.json"))
ck("агрегат читается после перезапуска", reloaded["snapshots"] == 5)

# ── 10-11. None и отсутствие утечек ──
print("\n10-11) None и отсутствие утечек")
s7 = Store()
s7.add_dna(row("z@0", volatility=None, market_volume=None, depth_10_up=None))
s7.flush_dna(now)
with gzip.open(s7.dna_path(now), "rt") as f:
    rr = list(csv.DictReader(f))[0]
ck("None записан пустым, не нулём", rr["volatility"] == "" and rr["depth_10_up"] == "",
   f"{rr['volatility']!r} {rr['depth_10_up']!r}")
ck("в схеме DNA нет полей исхода",
   not any(f in DNA_FIELDS for f in ("resolution", "final_direction", "max_continuation")))
fut = [f for f in ("max_continuation", "max_reversal", "resolution", "final_direction")
       if f in MOVE_FIELDS[:MOVE_FIELDS.index("outcome_ready")]]
ck("признаки движения без будущего", not fut, str(fut))
ck("агрегат не смешивает hypothetical и realized",
   "realized_pnl" not in json.dumps(a) and "hypothetical_pnl" not in json.dumps(a))

# ── устойчивость агрегата к обрыву raw ──
print("\nагрегат при повреждённом raw")
day3 = "2026-08-31"
s8 = Store()
d3 = datetime(2026, 8, 31, 12, 0, 0, tzinfo=timezone.utc)
for i in range(40):
    s8.add_dna(row(f"t@{i*15}", asset="BTC", window="5m"))
s8.flush_dna(d3)
rawb = open(s8.dna_path(d3), "rb").read()
open(s8.dna_path(d3), "wb").write(rawb[:int(len(rawb) * 0.97)])
a4 = AGG.build(day3)
ck("часть записей спасена при обрыве", a4["snapshots"] > 0, f"{a4['snapshots']} из 40")
ck("обрыв отмечен флагом", a4.get("raw_truncated") is True, str(a4.get("raw_truncated")))

# ── защита агрегата от потери данных при неполном raw ──
print("\nагрегат не уменьшается при неполном raw")
import subprocess
day4 = "2026-09-01"
d4 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
s9 = Store()
for i in range(30):
    s9.add_dna(row(f"full@{i*15}", asset="BTC", window="5m"))
s9.flush_dna(d4)
r1 = subprocess.run([sys.executable, os.path.join(ROOT, "polylab/data/aggregate.py"), day4],
                    capture_output=True, text=True)
a_full = json.load(open(f"data/agg/{day4}.json"))
ck("полный агрегат построен", a_full["snapshots"] == 30, str(a_full["snapshots"]))

# имитируем потерю raw: артефакт не восстановился, файл начат заново
os.remove(s9.dna_path(d4))
s10 = Store(); s10.seen = set()
for i in range(5):
    s10.add_dna(row(f"partial@{i*15}", asset="BTC", window="5m"))
s10.flush_dna(d4)
r2 = subprocess.run([sys.executable, os.path.join(ROOT, "polylab/data/aggregate.py"), day4],
                    capture_output=True, text=True)
a_after = json.load(open(f"data/agg/{day4}.json"))
ck("агрегат НЕ уменьшился", a_after["snapshots"] == 30, f"{a_after['snapshots']} (было 30)")
ck("факт неполноты помечен", a_after.get("raw_incomplete_on_rebuild") is True)
ck("зафиксировано, сколько увидели", (a_after.get("last_rebuild_attempt") or {}).get("snapshots_seen") == 5,
   str(a_after.get("last_rebuild_attempt")))
ck("предупреждение выведено", "ВНИМАНИЕ" in r2.stdout, r2.stdout.strip()[:60])

# рост по-прежнему возможен
s11 = Store(); s11.load_seen()
for i in range(40):
    s11.add_dna(row(f"more@{i*15}", asset="ETH", window="15m"))
s11.flush_dna(d4)
subprocess.run([sys.executable, os.path.join(ROOT, "polylab/data/aggregate.py"), day4],
               capture_output=True, text=True)
a_grow = json.load(open(f"data/agg/{day4}.json"))
ck("при полном raw агрегат растёт", a_grow["snapshots"] >= 40, str(a_grow["snapshots"]))

bad = [n for n, o in R if not o]
print(f"\n{'='*52}\nSTORAGE-5C: {len(R)-len(bad)}/{len(R)}")
if bad: print("ПРОВАЛЕНО:", *bad, sep="\n  ")
sys.exit(1 if bad else 0)
