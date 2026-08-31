#!/usr/bin/env python3
"""Прогон бэктеста: IS / OOS / сравнение с FORWARD.

Параметры стратегий подбираются ТОЛЬКО на in-sample и никогда на forward.
Forward здесь используется исключительно для сравнения, а не для настройки.
"""
from __future__ import annotations

import csv, json, os, sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from polylab.backtest.dataset import Dataset  # noqa: E402
from polylab.backtest.engine import ENGINE_VERSION, metrics, run, verdict  # noqa: E402
from polylab.strategies.momentum_late_window import MomentumLateWindow  # noqa: E402

OUT = "research/BACKTEST.json"


def forward_metrics(path: str = "data/signals.csv") -> dict:
    """FORWARD — реальные SHADOW-сигналы. Приводим к тому же формату метрик."""
    if not os.path.exists(path):
        return {"signals": 0, "closed": 0, "status": "NO_DATA",
                "note": "SHADOW-журнал пуст"}
    trades = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for r in csv.DictReader(f):
            if not r.get("resolution"):
                continue
            try:
                pnl = float(r.get("hypothetical_pnl") or 0)
            except ValueError:
                continue
            trades.append({"ts": r["ts"], "day": r["ts"][:10], "strategy": r.get("strategy"),
                           "window": r.get("window"), "asset": r.get("asset"),
                           "direction": r.get("direction"), "resolution": r["resolution"],
                           "pnl": pnl, "decision": r.get("decision")})
    return metrics(trades)


def main() -> None:
    ds = Dataset().load()
    st = ds.stats()
    is_mk, oos_mk, border = ds.split(float(os.getenv("OOS_FRACTION", "0.30")))

    factory = lambda: [MomentumLateWindow()]
    is_run = run(factory, is_mk, "BACKTEST", st["source"])
    oos_run = run(factory, oos_mk, "OOS", st["source"])
    fwd = forward_metrics()

    v = verdict(is_run["metrics"], oos_run["metrics"], fwd)
    rep = {
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "engine_version": ENGINE_VERSION, "dataset": st,
        "split": {"border_market_start": border, "is_windows": len(is_mk),
                  "oos_windows": len(oos_mk), "oos_fraction": float(os.getenv("OOS_FRACTION", "0.30")),
                  "rule": "разделение по времени, граница между окнами; подбор только на in-sample"},
        "in_sample": {k: v2 for k, v2 in is_run.items() if k != "trades"},
        "out_of_sample": {k: v2 for k, v2 in oos_run.items() if k != "trades"},
        "forward_shadow": fwd,
        "verdict": v,
        "guarantees": [
            "бэктест использует те же Arena/Portfolio, что и SHADOW",
            "исход окна берётся из последнего снимка; неизвестный исход исключается",
            "forward не участвует в подборе параметров",
            "результаты режимов не смешиваются",
        ],
    }
    os.makedirs("research", exist_ok=True)
    json.dump(rep, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    # сделки — отдельными файлами по режимам, чтобы их нельзя было перепутать
    os.makedirs("data/backtest", exist_ok=True)
    for name, r in (("in_sample", is_run), ("out_of_sample", oos_run)):
        if r["trades"]:
            keys = sorted({k for t in r["trades"] for k in t})
            with open(f"data/backtest/{name}.csv", "w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=keys)
                w.writeheader()
                for t in r["trades"]:
                    w.writerow(t)

    print(f"датасет: {st['rows_usable']} пригодных снимков, {st['unique_markets']} рынков, "
          f"{st['days']} дней, span {st['span_hours']} ч")
    for k in ("in_sample", "out_of_sample"):
        m = rep[k]["metrics"]
        print(f"{k:14} окон {rep[k]['windows_seen']:>4} · закрытых {m.get('closed',0):>3} · "
              f"статус {m.get('status')}")
    print(f"forward        закрытых {fwd.get('closed',0)} · статус {fwd.get('status')}")
    print(f"ВЕРДИКТ: {v['verdict']} — {'; '.join(v['reasons'])}")


if __name__ == "__main__":
    main()
