"""Тесты коллектора: дубли, рестарт, сбои API, порядок времени, пейсинг."""
import os, sys, time, tempfile, gzip, csv
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(tempfile.mkdtemp())

from polylab.data import sources as SRC
from polylab.data.store import Store, DNA_FIELDS
from polylab.core.features import snapshot_id

R = []
def ck(n, c, i=""):
    R.append((n, bool(c))); print(("  OK  " if c else " FAIL ") + n + ("  " + i if i else ""))

now = datetime.now(timezone.utc)

# ── 1. duplicate snapshot ──
print("\n1) дубликаты снимков")
s = Store()
row = lambda sid: {"snapshot_id": sid, "ts": now.isoformat(), "current_price": 100}
ck("первый записан", s.add_dna(row("m@100")) is True)
ck("повтор отклонён", s.add_dna(row("m@100")) is False)
ck("счётчик дублей", s.duplicates == 1, str(s.duplicates))
ck("в буфере одна запись", len(s._buf) == 1)

# ── 2 и 8. рестарт + идемпотентность ──
print("\n2) рестарт и идемпотентность")
s.add_dna(row("m@115")); s.flush_dna(now)
s2 = Store(); restored = s2.load_seen(now)
ck("id восстановлены", restored == 2, str(restored))
ck("после рестарта дубль отклонён", s2.add_dna(row("m@100")) is False)
ck("новый снимок принят", s2.add_dna(row("m@130")) is True)

# ── 18. повторный запуск на том же диапазоне ──
print("\n18) повторный прогон на том же временном диапазоне")
s2.flush_dna(now)
s3 = Store(); s3.load_seen(now)
before = sum(1 for _ in gzip.open(s3.dna_path(now), "rt"))
for sid in ("m@100", "m@115", "m@130"):
    s3.add_dna(row(sid))
s3.flush_dna(now)
after = sum(1 for _ in gzip.open(s3.dna_path(now), "rt"))
ck("файл не вырос", before == after, f"{before} → {after}")
ck("все три отклонены", s3.duplicates == 3, str(s3.duplicates))

# ── 3. timeout API ──
print("\n3) таймаут API")
import requests
calls = {"n": 0}
def boom(*a, **k):
    calls["n"] += 1
    raise requests.Timeout("timeout")
orig = SRC._S.get; SRC._S.get = boom
SRC.STATS.failures = 0; SRC.STATS.retries = 0
t0 = time.time(); res = SRC.get("https://x/y", tries=3); dt = time.time() - t0
SRC._S.get = orig
ck("вернул None, не упал", res is None)
ck("сделал 3 попытки", calls["n"] == 3, str(calls["n"]))
ck("посчитал отказ", SRC.STATS.failures == 1)
ck("экспоненциальная задержка (≥1.8с)", dt >= 1.7, f"{dt:.2f}с")

# ── 4. битый ответ ──
print("\n4) некорректный ответ API")
class FakeResp:
    status_code = 200
    def __init__(self, payload): self._p = payload
    def raise_for_status(self): pass
    def json(self):
        if isinstance(self._p, Exception): raise self._p
        return self._p
for name, payload in (("не JSON", ValueError("bad json")), ("None", None),
                      ("пустой список", []), ("строка вместо объекта", "oops")):
    SRC._S.get = lambda *a, _p=payload, **k: FakeResp(_p)
    try:
        out = SRC.get("https://x/y", tries=1)
        ok = True
    except Exception as e:
        ok = False; out = str(e)
    ck(f"выдержал {name}", ok, repr(out)[:40])
SRC._S.get = orig

# ── 5. пустой ответ свечей (реальный сценарий 5m) ──
print("\n5) пустой массив свечей — реальный случай 5m")
SRC._S.get = lambda *a, **k: FakeResp([])
SRC.STATS.failures = 0
ref = SRC.minute_ref("BTC", now)
SRC._S.get = orig
ck("HTTP 200 + пустой массив → None", ref is None)
ck("это НЕ засчитано как отказ API", SRC.STATS.failures == 0, str(SRC.STATS.failures))

# ── 13-14. reference price: точное ведро и повтор ──
print("\n13-14) опорная цена: точное ведро и повторная попытка")
start = now.replace(second=0, microsecond=0)
bucket = int(start.timestamp()) // 60 * 60
candles = [[bucket - 60, 1, 2, 100.0, 101.0, 5], [bucket, 1, 2, 200.0, 202.0, 5],
           [bucket + 60, 1, 2, 300.0, 303.0, 5]]
SRC._S.get = lambda *a, **k: FakeResp(candles)
ref = SRC.minute_ref("BTC", start)
SRC._S.get = orig
ck("взято ведро старта, не соседнее", ref == 201.0, str(ref))
SRC._S.get = lambda *a, **k: FakeResp([[bucket - 60, 1, 2, 100.0, 101.0, 5]])
ref2 = SRC.minute_ref("BTC", start)
SRC._S.get = orig
ck("нужного ведра нет → None, соседнее НЕ подставлено", ref2 is None, str(ref2))
seq = [FakeResp([]), FakeResp(candles)]
SRC._S.get = lambda *a, **k: seq.pop(0)
a1 = SRC.minute_ref("BTC", start); a2 = SRC.minute_ref("BTC", start)
SRC._S.get = orig
ck("первый проход пусто, второй заполнил", a1 is None and a2 == 201.0, f"{a1} → {a2}")

# ── 7. порядок времени ──
print("\n7) порядок времени")
ids = [snapshot_id("m", now.timestamp() + i * 15) for i in range(4)]
nums = [int(x.split("@")[1]) for x in ids]
ck("id монотонно возрастают", nums == sorted(nums), str(nums))
ck("разные ведра различаются", len(set(nums)) == 4)
# база выравнена по началу ведра, иначе +14с легально уходит в следующее
base = (now.timestamp() // 15) * 15
same = [snapshot_id("m", base + d) for d in (0, 1, 14.9)]
ck("внутри ведра id одинаков", len(set(same)) == 1, str(set(same)))
ck("+15с уже другое ведро", snapshot_id("m", base + 15) != same[0])

# ── 9. пейсинг ──
print("\n9) ограничение частоты запросов")
SRC._S.get = lambda *a, **k: FakeResp({"ok": 1})
SRC._last_call[0] = 0.0
t0 = time.time()
for _ in range(10): SRC.get("https://x/y", tries=1)
dt = time.time() - t0
SRC._S.get = orig
ck("паузы соблюдаются", dt >= 9 * SRC.MIN_INTERVAL * 0.9, f"{dt*1000:.0f}мс на 10 запросов")

bad = [n for n, o in R if not o]
print(f"\n{'='*52}\nCOLLECTOR: {len(R)-len(bad)}/{len(R)}")
if bad: print("ПРОВАЛЕНО:", *bad, sep="\n  ")
sys.exit(1 if bad else 0)
