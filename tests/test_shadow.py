"""Тесты SHADOW-арены: изоляция, отсутствие ордеров, hypothetical vs realized."""
import os, sys, csv, json, tempfile
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(tempfile.mkdtemp())

from polylab.core.shadow import ShadowRunner, MODE
from polylab.core.strategy import Strategy
from polylab.strategies.momentum_late_window import MomentumLateWindow

R = []
def ck(n, c, i=""):
    R.append((n, bool(c))); print(("  OK  " if c else " FAIL ") + n + ("  " + i if i else ""))

now = datetime.now(timezone.utc)
def mk(asset="BTC", el=.80, move=.0013, ask=.55, mid="m1", off=0, win=15):
    st = now - timedelta(seconds=win*60*el + off)
    return ({"market_id": mid, "asset": asset, "window_minutes": win, "start": st,
             "end": st + timedelta(minutes=win), "up_token": "u", "down_token": "d"},
            {"elapsed": el, "time_remaining": win*60*(1-el), "reference_price": 100.0,
             "current_price": 100*(1+move), "move": move, "volatility": .001,
             "acceleration": 1.1, "liquidity": 500},
            {"best_ask": ask, "best_bid": ask-.02, "spread": .02, "ask_depth": 300, "imbalance": .1},
            {"best_ask": round(1-ask, 2), "best_bid": .4, "spread": .03, "ask_depth": 200, "imbalance": -.1})

print("\nсквозная цепочка")
sr = ShadowRunner([MomentumLateWindow()], bankroll=1000.0)
res = sr.on_market(*mk(), now)
ck("сигнал создан", len(res) == 1)
sig, dec = res[0]
ck("стратегия названа", sig.strategy == "momentum_late_window")
ck("риск принял решение", dec.action in ("ACCEPT", "REDUCE"), dec.action)
ck("запись ждёт резолва", len(sr.ledger.pending) == 1)

print("\nордеров не существует")
row = sr.ledger.pending[0]["row"]
ck("execution_state = SHADOW", row["execution_state"] == "SHADOW", row["execution_state"])
ck("режим shadow", row["mode"] == MODE)
ck("filled_shares пуст", row["filled_shares"] == "")
ck("средняя цена исполнения пуста", row["average_fill_price"] == "")
ck("realized пуст до резолва", row["realized_pnl"] == "")
src = open(os.path.join(ROOT, "polylab/core/shadow.py")).read()
ck("в shadow нет вызова исполнения", "place_order" not in src and "post_order" not in src)
ck("LIVE в модуле отсутствует", "live" not in src.lower().replace("polylab", ""))

print("\nверсии зафиксированы")
for f in ("strategy_version", "config_version", "feature_version"):
    ck(f"{f} записан", row[f] not in ("", None), str(row[f]))

print("\nполнота полей ТЗ")
need = ["strategy", "ts", "asset", "market_id", "window", "direction", "entry_price",
        "reference_price", "move_pct", "elapsed", "confidence", "decision",
        "execution_state", "resolution", "hypothetical_pnl", "realized_pnl"]
miss = [f for f in need if f not in row]
ck("все обязательные поля присутствуют", not miss, str(miss))

print("\nрезолв и разделение hypothetical/realized")
for p in sr.ledger.pending: p["resolve_at"] = (now - timedelta(seconds=1)).isoformat()
n = sr.resolve(now, lambda a, dt: 101.0)
ck("резолв выполнен", n == 1, str(n))
rows = list(csv.DictReader(open("data/signals.csv")))
r = rows[0]
ck("исход записан", r["resolution"] in ("WIN", "LOSS"), r["resolution"])
ck("hypothetical заполнен", r["hypothetical_pnl"] not in ("", None), r["hypothetical_pnl"])
ck("realized ОСТАЁТСЯ пустым", r["realized_pnl"] == "", repr(r["realized_pnl"]))
ck("realized не подменён нулём", r["realized_pnl"] != "0" and r["realized_pnl"] != "0.0")
ck("банкролл не изменился в shadow", abs(sr.portfolio.bankroll - 1000.0) < 1e-9,
   str(sr.portfolio.bankroll))

print("\nриск-гейты работают")
sr2 = ShadowRunner([MomentumLateWindow()], bankroll=1000.0)
st = now - timedelta(seconds=15*60*.8)
a = sr2.on_market({"market_id": "w1", "asset": "BTC", "window_minutes": 15, "start": st,
                   "end": st+timedelta(minutes=15), "up_token": "u", "down_token": "d"},
                  mk()[1], mk()[2], mk()[3], now)
b = sr2.on_market({"market_id": "w2", "asset": "ETH", "window_minutes": 15, "start": st,
                   "end": st+timedelta(minutes=15), "up_token": "u", "down_token": "d"},
                  mk()[1], mk()[2], mk()[3], now)
ck("второй сигнал в том же окне отклонён", b and b[0][1].action == "REJECT", b[0][1].action if b else "нет")
ck("гейт назван", b and b[0][1].gate == "MAX_PER_WINDOW", b[0][1].gate if b else "")
ck("заблокированный тоже в журнале", len(sr2.ledger.pending) == 2, str(len(sr2.ledger.pending)))

print("\nизоляция стратегий")
class Broken(Strategy):
    name = "broken"; version = "0.0.1"
    def on_snapshot(self, s): raise RuntimeError("падаю")
sr3 = ShadowRunner([Broken(), MomentumLateWindow()], bankroll=1000.0)
r3 = sr3.on_market(*mk(mid="iso"), now)
ck("падение одной не мешает другой", len(r3) == 1 and r3[0][0].strategy == "momentum_late_window")
ck("ошибка зафиксирована", "broken" in (sr3.stats.get("errors") or {}), str(sr3.stats.get("errors")))

print("\nдубликаты и рестарт")
before = len(sr3.ledger.pending)
sr3.save()
sr4 = ShadowRunner([MomentumLateWindow()], bankroll=1000.0)
ck("очередь восстановлена", len(sr4.ledger.pending) == before, f"{len(sr4.ledger.pending)}/{before}")
ck("статистика восстановлена", sr4.stats["signals"] >= 1, str(sr4.stats["signals"]))

print("\nвыборка не выдаётся за доказательство")
H = open(os.path.join(ROOT, "docs/index.html"), encoding="utf-8").read()
ck("порог недостаточной выборки в арене", "INSUFFICIENT" in H and "res.length<100" in H.replace(" ", ""))
ck("нет слова profitable", "profitable" not in H.lower())

bad = [n for n, o in R if not o]
print(f"\n{'='*52}\nSHADOW: {len(R)-len(bad)}/{len(R)}")
if bad: print("ПРОВАЛЕНО:", *bad, sep="\n  ")
sys.exit(1 if bad else 0)
