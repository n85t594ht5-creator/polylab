"""Тесты PHASE 7: разделение выборок, отсутствие утечек, честные вердикты.

Данные здесь СИНТЕТИЧЕСКИЕ и помечены source=SYNTHETIC — они нужны только
для проверки корректности движка и никогда не смешиваются с REAL.
"""
import os, sys, csv, json, tempfile
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(tempfile.mkdtemp())

from polylab.backtest.dataset import Dataset
from polylab.backtest.engine import metrics, outcome_of, run, verdict, MIN_SAMPLE
from polylab.strategies.momentum_late_window import MomentumLateWindow

R = []
def ck(n, c, i=""):
    R.append((n, bool(c))); print(("  OK  " if c else " FAIL ") + n + ("  " + i if i else ""))

T0 = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)

def snaps(mid, day_off, up_won=True, ask=.55, move=.0013, win=15, asset="BTC"):
    """Окно из 4 снимков; последний — за 30 с до конца, поэтому исход известен."""
    start = T0 + timedelta(days=day_off, minutes=mid_i(mid) * 20)
    end = start + timedelta(minutes=win)
    out = []
    for k, (el, rem) in enumerate(((.78, 198), (.85, 135), (.92, 72), (.97, 27))):
        m = move if up_won else -move
        out.append({"ts": (start + timedelta(seconds=win*60*el)).isoformat(),
                    "start": start.isoformat(), "end": end.isoformat(),
                    "market_id": mid, "asset": asset, "window": f"{win}m",
                    "elapsed": el, "time_remaining": rem,
                    "reference_price": 100.0, "current_price": 100*(1+m), "move": m,
                    "up_ask": ask if m > 0 else round(1-ask, 2),
                    "down_ask": round(1-ask, 2) if m > 0 else ask,
                    "quality": "GOOD", "volatility": .001, "acceleration": 1.1})
    return out
def mid_i(mid): return int("".join(c for c in mid if c.isdigit()) or 0)

print("\nразделение выборок по времени")
ds = Dataset(source="SYNTHETIC")
ds.rows = [r for i in range(10) for r in snaps(f"m{i}", i)]
is_mk, oos_mk, border = ds.split(0.30)
ck("выборки не пересекаются", not (set(is_mk) & set(oos_mk)), str(set(is_mk) & set(oos_mk)))
ck("все окна распределены", len(is_mk) + len(oos_mk) == 10, f"{len(is_mk)}+{len(oos_mk)}")
ck("oos примерно 30%", 2 <= len(oos_mk) <= 4, str(len(oos_mk)))
is_last = max(v[0]["start"] for v in is_mk.values())
oos_first = min(v[0]["start"] for v in oos_mk.values())
ck("граница по времени: OOS строго позже IS", oos_first > is_last, f"{is_last} → {oos_first}")
ck("окно не разрезано между выборками",
   all(len({r["market_id"] for r in v}) == 1 for v in list(is_mk.values()) + list(oos_mk.values())))
ck("граница зафиксирована", border is not None)

print("\nисход окна не додумывается")
full = snaps("m1", 0)
ck("исход известен при свежем последнем снимке", outcome_of(full) is not None)
stale = [dict(r) for r in full]
stale[-1]["time_remaining"] = 400
ck("исход неизвестен, если снимок задолго до конца", outcome_of(stale[:-1] + [stale[-1]]) is None,
   str(outcome_of(stale)))
noref = [dict(r) for r in full]; noref[-1]["reference_price"] = ""
ck("нет опорной цены → исход неизвестен", outcome_of(noref) is None)
r0 = run(lambda: [MomentumLateWindow()], {"m1": stale}, "BACKTEST", "SYNTHETIC")
ck("окно с неизвестным исходом исключено", r0["windows_outcome_unknown"] == 1)
ck("сделок по нему нет", r0["metrics"]["closed"] == 0)

print("\nбэктест использует ту же арену")
src = open(os.path.join(ROOT, "polylab/backtest/engine.py")).read()
ck("импортирует Arena", "from polylab.core.strategy import Arena" in src)
ck("импортирует PortfolioManager", "from polylab.core.portfolio import PortfolioManager" in src)
ck("нет своей торговой логики", "min_move" not in src and "MIN_ELAPSED" not in src)

print("\nметрики и концентрация")
tr = [{"ts": f"2026-08-2{d}T10:00:00", "day": f"2026-08-2{d}", "strategy": "s", "window": "15m",
       "resolution": "WIN" if d == 1 else "LOSS", "pnl": 100.0 if d == 1 else -2.0} for d in range(1, 8)]
m = metrics(tr)
ck("считает winrate", abs(m["winrate"] - 1/7) < .01, str(m["winrate"]))
ck("находит лучший день", m["best_day"] == "2026-08-21", str(m["best_day"]))
ck("считает долю лучшего дня", m["best_day_share_pct"] > 100, str(m["best_day_share_pct"]))
ck("считает результат без лучшего дня", m["pnl_without_best_day"] < 0, str(m["pnl_without_best_day"]))
ck("малая выборка → INSUFFICIENT_SAMPLE", m["status"] == "INSUFFICIENT_SAMPLE", m["status"])
ck("причина названа", "нужно" in m["sample_note"])
big = [{"ts": f"2026-08-{10+i%20:02d}T10:00:00", "day": f"2026-08-{10+i%20:02d}", "strategy": "s",
        "window": "15m" if i % 2 else "5m", "resolution": "WIN" if i % 3 else "LOSS",
        "pnl": 3.0 if i % 3 else -2.0} for i in range(200)]
mb = metrics(big)
ck("достаточная выборка → SUPPORTED", mb["status"] == "SUPPORTED", mb["status"])
conc = big + [{"ts": "2026-09-01T10:00:00", "day": "2026-09-01", "strategy": "s", "window": "5m",
               "resolution": "WIN", "pnl": 5000.0}]
ck("один огромный день → CONCENTRATED", metrics(conc)["status"] == "CONCENTRATED",
   metrics(conc)["status"])
ck("разбивка по окнам есть", set(mb["by_window"]) == {"5m", "15m"}, str(set(mb["by_window"])))
ck("просадка считается", mb["max_drawdown"] >= 0)

print("\nвердикты по умолчанию осторожны")
ck("нет данных → INCONCLUSIVE",
   verdict({"status": "NO_DATA"}, {"status": "NO_DATA"})["verdict"] == "INCONCLUSIVE")
ck("малая выборка → INCONCLUSIVE",
   verdict({"status": "INSUFFICIENT_SAMPLE"}, {"status": "SUPPORTED"})["verdict"] == "INCONCLUSIVE")
ck("концентрация → NOT SUPPORTED",
   verdict({"status": "CONCENTRATED", "profit_factor": 5}, {"status": "SUPPORTED", "profit_factor": 2})["verdict"] == "NOT SUPPORTED")
v_deg = verdict({"status": "SUPPORTED", "profit_factor": 3}, {"status": "SUPPORTED", "profit_factor": .5})
ck("развал на OOS → NOT SUPPORTED", v_deg["verdict"] == "NOT SUPPORTED", v_deg["verdict"])
ck("причина развала названа", any("нетронутых" in r for r in v_deg["reasons"]))
v_ok = verdict({"status": "SUPPORTED", "profit_factor": 2}, {"status": "SUPPORTED", "profit_factor": 1.8},
               {"status": "SUPPORTED"})
ck("всё сошлось → SUPPORTED", v_ok["verdict"] == "SUPPORTED", v_ok["verdict"])
ck("forward мал → INCONCLUSIVE",
   verdict({"status": "SUPPORTED", "profit_factor": 2}, {"status": "SUPPORTED", "profit_factor": 2},
           {"status": "INSUFFICIENT_SAMPLE"})["verdict"] == "INCONCLUSIVE")

print("\nисточники данных не смешиваются")
r1 = run(lambda: [MomentumLateWindow()], is_mk, "BACKTEST", "SYNTHETIC")
ck("режим помечен в прогоне", r1["mode"] == "BACKTEST")
ck("источник помечен", r1["source"] == "SYNTHETIC")
ck("каждая сделка несёт режим и источник",
   all(t["mode"] == "BACKTEST" and t["source"] == "SYNTHETIC" for t in r1["trades"]),
   str(len(r1["trades"])))
r2 = run(lambda: [MomentumLateWindow()], oos_mk, "OOS", "SYNTHETIC")
ck("разные прогоны не делят состояние", r1["metrics"]["closed"] != r2["metrics"]["closed"]
   or set(t["ts"] for t in r1["trades"]).isdisjoint(t["ts"] for t in r2["trades"]))
ck("стратегия и версия записаны",
   all(t.get("strategy") and t.get("strategy_version") for t in r1["trades"]))

print("\nforward не участвует в подборе")
run_src = open(os.path.join(ROOT, "polylab/backtest/run.py")).read()
ck("forward считается отдельной функцией", "def forward_metrics" in run_src)
ck("параметры не подбираются по forward",
   "fwd" not in run_src.split("factory =")[1].split("is_run")[0])
ck("правило зафиксировано в отчёте", "forward не участвует в подборе" in run_src)

bad = [n for n, o in R if not o]
print(f"\n{'='*52}\nBACKTEST: {len(R)-len(bad)}/{len(R)}")
if bad: print("ПРОВАЛЕНО:", *bad, sep="\n  ")
sys.exit(1 if bad else 0)
