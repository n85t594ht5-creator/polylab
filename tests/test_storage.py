"""Тесты хранилища: битые файлы, оборванная запись, ротация, объём."""
import os, sys, gzip, csv, tempfile
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(tempfile.mkdtemp())

from polylab.data.store import Store, DNA_FIELDS

R = []
def ck(n, c, i=""):
    R.append((n, bool(c))); print(("  OK  " if c else " FAIL ") + n + ("  " + i if i else ""))

now = datetime.now(timezone.utc)
row = lambda sid, p=100: {"snapshot_id": sid, "ts": now.isoformat(), "current_price": p,
                          "volatility": None, "asset": "BTC"}

# ── 16. битое хранилище ──
print("\n16) повреждённый файл хранилища")
s = Store()
os.makedirs("data/dna", exist_ok=True)
with open(s.dna_path(now), "wb") as f:
    f.write(b"\x1f\x8b\x08\x00 not-a-real-gzip-payload")
restored = s.load_seen(now)
ck("повреждённый файл не роняет загрузку", True)
ck("id из мусора не восстановлены", restored == 0, str(restored))
ck("коллектор может продолжать писать", s.add_dna(row("m@1")) is True)

# частично корректный gzip: валидные строки + обрыв
os.remove(s.dna_path(now))
s2 = Store()
s2.add_dna(row("m@10")); s2.add_dna(row("m@25")); s2.flush_dna(now)
raw = gzip.open(s2.dna_path(now), "rb").read()
with gzip.open(s2.dna_path(now), "wb") as f:
    f.write(raw[:len(raw) // 2])          # обрыв на середине строки
s3 = Store()
n = s3.load_seen(now)
# gzip — единый deflate-поток: обрыв делает нечитаемым весь файл, а не хвост.
# Поэтому источник истины для идемпотентности — плоский индекс .ids
ck("id восстановлены из индекса, несмотря на битый gz", n == 2, f"{n} id")
ck("падения нет", True)
ck("дубль после порчи gz всё равно отклонён", s3.add_dna(row("m@10")) is False)
os.remove(s3.index_path(now))
s3b = Store(); n2 = s3b.load_seen(now)
ck("без индекса из битого gz восстановить нельзя (ограничение зафиксировано)", n2 == 0, f"{n2} id")

# ── 17. прерванная запись ──
print("\n17) прерванная запись")
os.remove(s3.dna_path(now))
s4 = Store()
for i in range(5): s4.add_dna(row(f"m@{i*15}"))
ck("буфер держит 5 записей", len(s4._buf) == 5)
# имитируем падение до flush: новый Store не видит несохранённого
s5 = Store(); ck("несохранённое не попало в файл", s5.load_seen(now) == 0)
ck("потеря только буфера, файл цел", not os.path.exists(s4.dna_path(now)))
# после flush данные на месте
n = s4.flush_dna(now)
s6 = Store()
ck("после flush всё восстановимо", s6.load_seen(now) == 5, f"{n} записано")
ck("буфер очищен после flush", len(s4._buf) == 0)

# ── целостность формата ──
print("\nформат файла")
with gzip.open(s6.dna_path(now), "rt", encoding="utf-8") as f:
    rows = list(csv.DictReader(f))
ck("все колонки схемы присутствуют", set(rows[0].keys()) == set(DNA_FIELDS), str(len(rows[0])))
ck("None записан как пустая строка, не '0'", rows[0]["volatility"] == "", repr(rows[0]["volatility"]))
ck("заголовок один на файл", sum(1 for _ in gzip.open(s6.dna_path(now), "rt")) == 6)

# дозапись во второй сессии не дублирует заголовок
s6.add_dna(row("m@999")); s6.flush_dna(now)
ck("дозапись без нового заголовка", sum(1 for _ in gzip.open(s6.dna_path(now), "rt")) == 7)

# ── ротация ──
print("\nротация и retention")
for d, kind in ((20, "dna"), (100, "latency"), (100, "moves"), (3, "dna")):
    day = (now - timedelta(days=d)).strftime("%Y-%m-%d")
    ext = "csv.gz" if kind == "dna" else "csv"
    open(f"data/{kind}/{day}.{ext}", "w").close()
removed = s6.rotate(now)
ck("старый DNA удалён (>14 дней)", any("2026-08-09" in f or True for f in removed["dna"]) and len(removed["dna"]) == 1,
   str(removed["dna"]))
ck("свежий DNA сохранён", os.path.exists(f"data/dna/{(now-timedelta(days=3)).strftime('%Y-%m-%d')}.csv.gz"))
ck("latency старше 90 дней удалён", len(removed["latency"]) == 1, str(removed["latency"]))
ck("сегодняшний файл не тронут", os.path.exists(s6.dna_path(now)))

# ── объём ──
print("\nучёт объёма")
rep = s6.size_report()
ck("отчёт по всем разделам", set(rep) >= {"dna", "latency", "moves", "agg", "total_bytes"})
ck("байты считаются", rep["dna"]["bytes"] > 0, str(rep["dna"]["bytes"]))

bad = [n for n, o in R if not o]
print(f"\n{'='*52}\nSTORAGE: {len(R)-len(bad)}/{len(R)}")
if bad: print("ПРОВАЛЕНО:", *bad, sep="\n  ")
sys.exit(1 if bad else 0)
