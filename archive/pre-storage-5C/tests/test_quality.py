"""Тесты качества данных: маркировка статусов, стакан, отсутствующие уровни."""
import os, sys, tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(tempfile.mkdtemp())

from polylab.data.sources import book_metrics
from polylab.core.features import quality, build_features, realized_vol, acceleration

R = []
def ck(n, c, i=""):
    R.append((n, bool(c))); print(("  OK  " if c else " FAIL ") + n + ("  " + i if i else ""))

# ── 11. разбор глубины стакана ──
print("\n11) разбор глубины стакана")
raw = {"asks": [{"price": "0.55", "size": "100"}, {"price": "0.56", "size": "50"},
                {"price": "0.57", "size": "40"}, {"price": "0.58", "size": "30"},
                {"price": "0.59", "size": "20"}, {"price": "0.60", "size": "10"}],
       "bids": [{"price": "0.52", "size": "80"}, {"price": "0.51", "size": "60"}],
       "timestamp": "1787993409921", "hash": "abc", "last_trade_price": "0.54"}
m = book_metrics(raw)
ck("best_ask — минимальная цена ask", m["best_ask"] == 0.55, str(m["best_ask"]))
ck("best_bid — максимальная цена bid", m["best_bid"] == 0.52, str(m["best_bid"]))
ck("спред считается", abs(m["spread"] - 0.03) < 1e-9, str(m["spread"]))
ck("depth_1 = цена×размер", abs(m["depth_1"] - 55.0) < 1e-9, str(m["depth_1"]))
ck("depth_2 кумулятивно", abs(m["depth_2"] - 83.0) < 1e-9, str(m["depth_2"]))
ck("depth_5 кумулятивно", abs(m["depth_5"] - 135.0) < 0.01, str(m["depth_5"]))
ck("timestamp стакана прочитан", m["book_ts"] == 1787993409921.0)
ck("дисбаланс в диапазоне −1..1", -1 <= m["imbalance"] <= 1, str(m["imbalance"]))
ck("уровни посчитаны", m["n_ask_levels"] == 6 and m["n_bid_levels"] == 2)

# ── 12. отсутствующие уровни → None, НЕ 0 ──
print("\n12) отсутствующие уровни дают None, а не ноль")
ck("depth_10 при 6 уровнях → None", m["depth_10"] is None, repr(m["depth_10"]))
ck("это именно None, не 0", m["depth_10"] is not 0 and m["depth_10"] != 0.0)
empty = book_metrics(None)
for f in ("best_ask", "best_bid", "spread", "depth_1", "imbalance", "book_ts"):
    ck(f"пустой стакан: {f} = None", empty[f] is None, repr(empty[f]))
one_side = book_metrics({"asks": [{"price": "0.55", "size": "10"}], "bids": []})
ck("нет bid → spread None", one_side["spread"] is None)
ck("нет bid → bid_depth None, не 0", one_side["bid_depth"] is None, repr(one_side["bid_depth"]))
ck("но ask_depth посчитан", one_side["ask_depth"] == 5.5, str(one_side["ask_depth"]))
broken = book_metrics({"asks": [{"price": "abc", "size": "10"}, {"price": "0.55", "size": "x"}], "bids": []})
ck("нечисловые уровни отброшены, не обнулены", broken["best_ask"] is None, repr(broken["best_ask"]))

# ── 15. маркировка статусов ──
print("\n15) маркировка качества")
full = {"current_price": 100, "reference_price": 99, "up_ask": .55, "down_ask": .45,
        "volatility": .001, "acceleration": 1.0, "spread_up": .02, "imbalance_up": .1,
        "depth_1_up": 50, "book_ts_up": 1}
ck("всё на месте → GOOD", quality(full, 100)[0] == "GOOD", quality(full, 100)[0])
ck("нет цены → INVALID", quality({**full, "current_price": None}, 100)[0] == "INVALID")
ck("нет reference → INVALID", quality({**full, "reference_price": None}, 100)[0] == "INVALID")
ck("нет ask исхода → INCOMPLETE", quality({**full, "up_ask": None}, 100)[0] == "INCOMPLETE")
ck("нет волатильности → DEGRADED", quality({**full, "volatility": None}, 100)[0] == "DEGRADED")
ck("устаревший стакан → STALE", quality(full, 90_000)[0] == "STALE")
ck("INVALID важнее STALE", quality({**full, "current_price": None}, 90_000)[0] == "INVALID")
st, missing = quality({**full, "volatility": None, "spread_up": None}, 100)
ck("список пустых полей ведётся", set(missing) == {"volatility", "spread_up"}, str(missing))

# ── признаки при нехватке истории ──
print("\nпризнаки при короткой истории")
ck("мало точек → volatility None, не 0", realized_vol([100, 101]) is None)
ck("мало точек → acceleration None, не 0", acceleration([100, 101]) is None)
f = build_features([{"current_price": 100}], {"liquidity": 5.0}, {}, {})
ck("build_features не подставляет нули", f["volatility"] is None and f["acceleration"] is None)
ck("объём Polymarket всегда None (источник не отдаёт)", f["market_volume"] is None)
ck("ликвидность пробрасывается как есть", f["liquidity"] == 5.0)
ck("версия признаков записана", f["feature_version"] == "1")

bad = [n for n, o in R if not o]
print(f"\n{'='*52}\nQUALITY: {len(R)-len(bad)}/{len(R)}")
if bad: print("ПРОВАЛЕНО:", *bad, sep="\n  ")
sys.exit(1 if bad else 0)
