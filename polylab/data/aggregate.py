#!/usr/bin/env python3
"""Дневной агрегат POLYLAB.

Сжимает суточный raw в компактный JSON, который переживёт удаление raw.
Только те разрезы, которые нужны для исследования, — без полей «на всякий случай».

Детерминированность: один и тот же raw даёт байт-в-байт одинаковый агрегат.
"""
from __future__ import annotations

import csv
import gzip
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from polylab.data.store import Store, read_gz_rows  # noqa: E402

AGG_VERSION = "1"


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _bucket(v, edges, labels, last):
    if v is None:
        return None
    for e, l in zip(edges, labels):
        if v < e:
            return l
    return last


def entry_bucket(p):
    return _bucket(p, (0.5, 0.55, 0.6, 0.62), ("<0.50", "0.50–0.55", "0.55–0.60", "0.60–0.62"), ">0.62")


def move_bucket(m):
    if m is None:
        return None
    return _bucket(abs(m), (0.0005, 0.001, 0.0012, 0.0015, 0.002),
                   ("0.00–0.05%", "0.05–0.10%", "0.10–0.12%", "0.12–0.15%", "0.15–0.20%"), "0.20%+")


def elapsed_bucket(e):
    return _bucket(e, (0.5, 0.6, 0.75, 0.85, 0.95),
                   ("<50%", "50–60%", "60–75%", "75–85%", "85–95%"), "95%+")


def _blank():
    return {"n": 0, "valid": 0, "quality": defaultdict(int), "missing": defaultdict(int)}


def build(day: str, root: str = "data") -> dict:
    """Собирает агрегат за сутки. Отсутствующие значения не превращаются в нули."""
    s = Store(root)
    dna_p = os.path.join(root, "raw", "dna", f"{day}.csv.gz")
    # устойчивое чтение: при обрыве gzip берём всё, что успело распаковаться,
    # иначе повреждение хвоста стоило бы нам агрегата за целые сутки
    rows, truncated = read_gz_rows(dna_p)

    cuts: dict = {k: defaultdict(_blank) for k in
                  ("window", "asset", "entry_bucket", "move_bucket", "elapsed_bucket", "quality")}
    totals = _blank()
    feat_avail = defaultdict(int)
    FEATS = ("volatility", "acceleration", "spread_up", "imbalance_up", "depth_1_up",
             "depth_10_up", "book_ts_up", "liquidity", "market_volume")

    for r in rows:
        q = r.get("quality") or "UNKNOWN"
        valid = q not in ("INVALID",)
        move, elapsed = _f(r.get("move")), _f(r.get("elapsed"))
        up = _f(r.get("up_ask"))
        keys = {"window": r.get("window") or "?", "asset": r.get("asset") or "?",
                "entry_bucket": entry_bucket(up), "move_bucket": move_bucket(move),
                "elapsed_bucket": elapsed_bucket(elapsed), "quality": q}
        totals["n"] += 1
        totals["valid"] += valid
        totals["quality"][q] += 1
        for m in (r.get("missing") or "").split("|"):
            if m:
                totals["missing"][m] += 1
        for cut, key in keys.items():
            if key is None:
                continue
            d = cuts[cut][key]
            d["n"] += 1
            d["valid"] += valid
            d["quality"][q] += 1
        for f in FEATS:
            if r.get(f) not in ("", None):
                feat_avail[f] += 1

    # движения с исходами — размеченный датасет
    moves_p = os.path.join(root, "moves", f"{day}.csv")
    mv = {"n": 0, "with_outcome": 0, "continued": 0, "reversed": 0,
          "by_window": defaultdict(lambda: {"n": 0, "continued": 0, "reversed": 0}),
          "avg_time_to_reversal_sec": None}
    trevs = []
    if os.path.exists(moves_p):
        with open(moves_p, encoding="utf-8", errors="replace") as f:
            for r in csv.DictReader(f):
                mv["n"] += 1
                if r.get("outcome_ready") == "1":
                    mv["with_outcome"] += 1
                    res = r.get("resolution")
                    w = mv["by_window"][r.get("window") or "?"]
                    w["n"] += 1
                    if res == "CONTINUED":
                        mv["continued"] += 1; w["continued"] += 1
                    elif res == "REVERSED":
                        mv["reversed"] += 1; w["reversed"] += 1
                    t = _f(r.get("time_to_reversal_sec"))
                    if t is not None:
                        trevs.append(t)
    if trevs:
        mv["avg_time_to_reversal_sec"] = round(sum(trevs) / len(trevs), 1)

    lat_p = os.path.join(root, "raw", "latency", f"{day}.csv")
    lat = {"n": 0, "quality": defaultdict(int)}
    if os.path.exists(lat_p):
        with open(lat_p, encoding="utf-8", errors="replace") as f:
            for r in csv.DictReader(f):
                lat["n"] += 1
                lat["quality"][r.get("quality") or "?"] += 1

    def undefault(o):
        if isinstance(o, defaultdict):
            o = dict(o)
        if isinstance(o, dict):
            return {k: undefault(v) for k, v in sorted(o.items(), key=lambda x: str(x[0]))}
        return o

    return undefault({
        "day": day, "agg_version": AGG_VERSION,
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "snapshots": totals["n"], "valid": totals["valid"],
        "quality": totals["quality"], "missing_fields": totals["missing"],
        "feature_availability": {k: feat_avail.get(k, 0) for k in FEATS},
        "cuts": {c: {k: {"n": v["n"], "valid": v["valid"], "quality": v["quality"]}
                     for k, v in d.items()} for c, d in cuts.items()},
        "moves": mv, "latency": lat,
        "raw_present": os.path.exists(dna_p),
        "raw_truncated": truncated,
    })


def main() -> None:
    day = sys.argv[1] if len(sys.argv) > 1 else datetime.now(timezone.utc).strftime("%Y-%m-%d")
    agg = build(day)
    os.makedirs("data/agg", exist_ok=True)
    p = f"data/agg/{day}.json"

    # Защита долгосрочного датасета: raw живёт в артефактах и может не
    # восстановиться. Если новый расчёт беднее уже сохранённого — сохраняем
    # прежний и помечаем факт, а не молча теряем сутки.
    if os.path.exists(p):
        try:
            old = json.load(open(p, encoding="utf-8"))
        except Exception:
            old = None
        if old and old.get("snapshots", 0) > agg.get("snapshots", 0):
            print(f"ВНИМАНИЕ: пересчёт дал {agg['snapshots']} снимков против {old['snapshots']} "
                  f"сохранённых — raw неполон. Оставляю прежний агрегат.")
            old["raw_incomplete_on_rebuild"] = True
            old["last_rebuild_attempt"] = {
                "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "snapshots_seen": agg.get("snapshots", 0)}
            agg = old
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(agg, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.flush(); os.fsync(f.fileno())
    os.replace(tmp, p)          # атомарная запись
    # индекс дней для панели
    idx = sorted(f[:-5] for f in os.listdir("data/agg") if f.endswith(".json") and f != "index.json")
    days = []
    for d in idx:
        try:
            a = json.load(open(f"data/agg/{d}.json"))
            days.append({"day": d, "snapshots": a.get("snapshots", 0), "valid": a.get("valid", 0),
                         "moves": (a.get("moves") or {}).get("with_outcome", 0),
                         "raw_present": a.get("raw_present", False)})
        except Exception:
            pass
    json.dump({"days": days, "agg_version": AGG_VERSION}, open("data/agg/index.json", "w"),
              ensure_ascii=False, indent=1)
    print(f"агрегат {day}: снимков {agg['snapshots']}, валидных {agg['valid']}, "
          f"движений с исходом {agg['moves']['with_outcome']}, размер {os.path.getsize(p)} Б")


if __name__ == "__main__":
    main()
