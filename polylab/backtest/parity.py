#!/usr/bin/env python3
"""PHASE 9.1 — почему реплей молчит там, где SHADOW находит сигналы.

Ничего не чинит. Измеряет фактическое состояние записанных снимков в тот момент,
когда стратегия принимает решение, и сравнивает с реальными SHADOW-сигналами.

Условия входа стратегии здесь НЕ дублируются: мы вызываем саму стратегию, а при
отказе записываем сырые значения, чтобы распределение было видно человеку.
"""
from __future__ import annotations

import csv, json, os, sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from polylab.backtest.dataset import Dataset  # noqa: E402
from polylab.backtest.engine import _f, _snap  # noqa: E402
from polylab.strategies.momentum_late_window import MomentumLateWindow  # noqa: E402

OUT = "research/PARITY.json"


def bucket(v, edges):
    if v is None:
        return "none"
    for e in edges:
        if v < e:
            return f"<{e}"
    return f">={edges[-1]}"


def main() -> None:
    ds = Dataset().load()
    strat = MomentumLateWindow()
    p = strat.params

    stats = {"snapshots": 0, "late": 0, "signals": 0,
             "ask_side_present": 0, "ask_side_missing": 0,
             "ask_dist_late": defaultdict(int), "move_dist_late": defaultdict(int),
             "elapsed_dist": defaultdict(int), "quality_late": defaultdict(int),
             "both_asks_present": 0, "one_ask_present": 0, "no_ask": 0}

    for r in ds.rows:
        try:
            snap = _snap(r, 1000.0)
        except Exception:
            continue
        stats["snapshots"] += 1
        stats["elapsed_dist"][bucket(snap.elapsed, [0.5, 0.6, 0.75, 0.85, 0.95])] += 1
        up, dn = snap.up_ask, snap.down_ask
        stats["both_asks_present"] += int(up is not None and dn is not None)
        stats["one_ask_present"] += int((up is None) != (dn is None))
        stats["no_ask"] += int(up is None and dn is None)

        if snap.elapsed < p["min_elapsed"] or snap.remaining_sec < p["min_remaining_sec"]:
            continue
        stats["late"] += 1
        stats["quality_late"][r.get("quality") or "?"] += 1
        side_ask = snap.ask_for("UP" if snap.move > 0 else "DOWN")
        if side_ask is None:
            stats["ask_side_missing"] += 1
        else:
            stats["ask_side_present"] += 1
            stats["ask_dist_late"][bucket(side_ask, [0.3, 0.5, 0.55, 0.62, 0.75])] += 1
        stats["move_dist_late"][bucket(abs(snap.move), [0.0005, 0.001, 0.0012, 0.002])] += 1
        if strat.on_snapshot(snap):
            stats["signals"] += 1

    # что реально записал SHADOW за то же время
    shadow = {"signals": 0, "entry_dist": defaultdict(int), "elapsed_dist": defaultdict(int)}
    if os.path.exists("data/signals.csv"):
        with open("data/signals.csv", encoding="utf-8", errors="replace") as f:
            for row in csv.DictReader(f):
                shadow["signals"] += 1
                shadow["entry_dist"][bucket(_f(row.get("entry_price")), [0.3, 0.5, 0.55, 0.62, 0.75])] += 1
                shadow["elapsed_dist"][bucket(_f(row.get("elapsed")), [0.5, 0.6, 0.75, 0.85, 0.95])] += 1

    und = lambda d: {k: dict(v) if isinstance(v, defaultdict) else v for k, v in d.items()}
    rep = {"built_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
           "dataset": ds.stats(), "replay": und(stats), "shadow": und(shadow),
           "strategy_params": p,
           "diagnosis": ""}

    late = stats["late"] or 1
    miss = stats["ask_side_missing"] / late
    if stats["signals"] == 0 and shadow["signals"] > 0:
        if miss > 0.3:
            rep["diagnosis"] = (f"в {miss*100:.0f}% поздних снимков нет цены нужной стороны — "
                                "реплей не может воспроизвести решение")
        else:
            rep["diagnosis"] = ("цены есть, но распределение записанных ask не попадает в зону входа: "
                               "см. ask_dist_late против shadow.entry_dist")
    elif stats["signals"]:
        rep["diagnosis"] = f"реплей воспроизводит сигналы: {stats['signals']}"
    else:
        rep["diagnosis"] = "сигналов нет ни в реплее, ни в SHADOW"

    os.makedirs("research", exist_ok=True)
    json.dump(rep, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)
    print(f"снимков {stats['snapshots']} · поздних {stats['late']} · сигналов в реплее {stats['signals']}")
    print(f"цена нужной стороны: есть {stats['ask_side_present']}, нет {stats['ask_side_missing']}")
    print(f"обе цены: {stats['both_asks_present']}, одна: {stats['one_ask_present']}, ни одной: {stats['no_ask']}")
    print("ask в поздней фазе:", dict(stats["ask_dist_late"]))
    print("SHADOW entry:", dict(shadow["entry_dist"]), "| сигналов:", shadow["signals"])
    print("ДИАГНОЗ:", rep["diagnosis"])


if __name__ == "__main__":
    main()
