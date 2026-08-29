"""Ключевой инвариант POLYLAB: будущее не попадает в признаки прошлого."""
import os, sys, tempfile, csv
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(tempfile.mkdtemp())

from polylab.core.types import MarketSnapshot, Outcome
from polylab.core.features import build_features, realized_vol, acceleration, reversals
from polylab.data.store import MOVE_FIELDS, Store

R = []
def ck(n, c, i=""):
    R.append((n, bool(c))); print(("  OK  " if c else " FAIL ") + n + ("  " + i if i else ""))

t1 = datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc)
t2 = t1 + timedelta(minutes=15)

# ── главный сценарий: снимок T1, исход после T2 ──
print("\nинвариант: features(T1) не меняются после появления Outcome(T2)")
hist = [{"current_price": p} for p in (100, 100.1, 100.2, 100.15, 100.3, 100.4)]
feats_t1 = build_features(hist, {"liquidity": 10.0}, {"spread": .02}, {"spread": .03})
snap = MarketSnapshot(ts=t1, asset="BTC", market_id="m1", window_minutes=15, start=t1,
                      end=t2, elapsed=0.8, remaining_sec=180, reference_price=100.0,
                      current_price=100.4, move=0.004, features=feats_t1)
snapshot_before = dict(snap.features)
import copy
frozen_copy = copy.deepcopy(snapshot_before)

out = Outcome(snapshot_id="m1@1000", resolved_at=t2, final_price=101.0,
              final_direction="UP", resolution="CONTINUED",
              max_continuation=0.01, max_reversal=-0.002, time_to_reversal_sec=60)

ck("features(T1) не изменились после Outcome", snap.features == frozen_copy)
ck("Outcome существует отдельно", out.snapshot_id == "m1@1000" and out.resolution == "CONTINUED")
ck("Outcome появился позже снимка", out.resolved_at > snap.ts)

forbidden = Outcome.FORBIDDEN_IN_FEATURES
leaked = [k for k in forbidden if k in snap.features]
ck("ни одно поле исхода не попало в признаки", not leaked, str(leaked))

# ── снимок неизменяем на уровне типа ──
print("\nснимок физически неизменяем")
try:
    snap.current_price = 999.0
    ck("присвоение в снимок запрещено", False, "изменение прошло!")
except FrozenInstanceError:
    ck("присвоение в снимок запрещено", True)
try:
    out.resolution = "LOSS"
    ck("исход тоже неизменяем", False)
except FrozenInstanceError:
    ck("исход тоже неизменяем", True)

# ── функции признаков не принимают будущее ──
print("\nфункции признаков видят только прошлое")
past = [100, 100.1, 100.2, 100.15, 100.3, 100.4]
future = past + [150.0, 90.0]
ck("volatility по прошлому ≠ по будущему", realized_vol(past) != realized_vol(future))
ck("acceleration по прошлому ≠ по будущему", acceleration(past) != acceleration(future))
f_now = build_features([{"current_price": p} for p in past], {}, {}, {})
f_fut = build_features([{"current_price": p} for p in future], {}, {}, {})
ck("вектор признаков зависит только от переданной истории", f_now != f_fut)
ck("build_features не имеет доступа к исходу",
   "resolution" not in f_now and "final_direction" not in f_now)

# ── схема журнала движений разделяет группы ──
print("\nсхема moves: признаки и исход разделены")
feature_fields = MOVE_FIELDS[:MOVE_FIELDS.index("outcome_ready")]
outcome_fields = MOVE_FIELDS[MOVE_FIELDS.index("outcome_ready"):]
ck("признаки не содержат будущих полей",
   not any(f in feature_fields for f in ("max_continuation", "max_reversal", "resolution", "final_direction")),
   str([f for f in feature_fields if f in outcome_fields]))
ck("исход содержит именно будущие поля",
   all(f in outcome_fields for f in ("max_continuation", "max_reversal", "resolution", "final_direction")))
ck("есть флаг готовности исхода", "outcome_ready" in MOVE_FIELDS)

# ── запись движения в момент обнаружения не содержит исхода ──
print("\nзапись в момент обнаружения")
s = Store()
detect_row = {"move_id": "x1", "detected_at": t1.isoformat(), "market_id": "m1", "asset": "BTC",
              "window": "15m", "initial_move": 0.001, "duration_so_far_sec": 60,
              "outcome_ready": 0}
s.append("moves", MOVE_FIELDS, detect_row, t1)
rows = list(csv.DictReader(open(f"data/moves/{t1.strftime('%Y-%m-%d')}.csv", encoding="utf-8")))
r = rows[0]
ck("outcome_ready = 0", r["outcome_ready"] == "0")
ck("поля исхода пусты, не нули",
   all(r[f] == "" for f in ("max_continuation", "max_reversal", "resolution", "final_direction")),
   str({f: r[f] for f in ("max_continuation", "resolution")}))

bad = [n for n, o in R if not o]
print(f"\n{'='*52}\nLEAKAGE: {len(R)-len(bad)}/{len(R)}")
if bad: print("ПРОВАЛЕНО:", *bad, sep="\n  ")
sys.exit(1 if bad else 0)
