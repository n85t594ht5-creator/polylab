#!/usr/bin/env python3
"""Сборка данных для дашборда: docs/data/.

Панель читает только то, что здесь опубликовано. Ничего не досочиняет:
если источника нет — в манифесте стоит honest-статус, и страница показывает его,
а не пустой график с нулями.
"""
from __future__ import annotations

import csv, json, os, sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from polylab.data.store import read_gz_rows  # noqa: E402

PUBLISH_VERSION = "1"
OUT = "docs/data"
DNA_SAMPLE_LIMIT = int(os.getenv("DNA_SAMPLE_LIMIT", "800"))

# Поля снимка, нужные графикам Market DNA и Order Book. venue/instrument —
# абстрактные, чтобы страница не была привязана к Polymarket.
DNA_KEEP = ["ts", "asset", "window", "market_id", "slug", "elapsed", "time_remaining",
            "reference_price", "current_price", "move", "direction",
            "up_ask", "down_ask", "spread_up", "spread_dn",
            "bid_depth_up", "ask_depth_up", "imbalance_up",
            "depth_1_up", "depth_10_up", "liquidity",
            "volatility", "acceleration", "reversals_so_far", "quality"]


def _num(v):
    if v in ("", None):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return v


def dna_sample(root="data", limit=DNA_SAMPLE_LIMIT) -> dict:
    """Последние N снимков для графиков. Источник — локальный raw (он есть только
    во время прогона коллектора; позже уезжает в артефакт)."""
    d = os.path.join(root, "raw", "dna")
    files = sorted(os.listdir(d)) if os.path.isdir(d) else []
    rows: list[dict] = []
    for fn in files[-3:]:
        r, _ = read_gz_rows(os.path.join(d, fn))
        rows.extend(r)
    if not rows:
        return {"status": "NO_LOCAL_RAW", "rows": [], "note":
                "сырые снимки хранятся в артефактах Actions; график строится по последней выгрузке"}
    rows = rows[-limit:]
    out = []
    for r in rows:
        o = {k: _num(r.get(k)) for k in DNA_KEEP}
        o["venue"] = "polymarket"
        o["instrument"] = r.get("asset")
        out.append(o)
    return {"status": "OK", "rows": out, "count": len(out),
            "fields": DNA_KEEP + ["venue", "instrument"]}


def moves(root="data") -> dict:
    d = os.path.join(root, "moves")
    files = sorted(os.listdir(d)) if os.path.isdir(d) else []
    rows = []
    for fn in files[-30:]:
        with open(os.path.join(d, fn), encoding="utf-8", errors="replace") as f:
            rows.extend(list(csv.DictReader(f)))
    if not rows:
        return {"status": "NO_DATA", "rows": [],
                "note": "движения ещё не зафиксированы"}
    for r in rows:
        for k in ("initial_move", "max_continuation", "max_reversal",
                  "time_to_reversal_sec", "move_duration_sec", "acceleration_so_far",
                  "elapsed_at_detect", "entry_ask_at_detect"):
            r[k] = _num(r.get(k))
    return {"status": "OK", "rows": rows[-500:], "count": len(rows)}


def signals(root="data") -> dict:
    p = os.path.join(root, "signals.csv")
    if not os.path.exists(p):
        return {"status": "NO_DATA", "rows": [],
                "note": "арена стратегий в проде ещё не запускалась — журнал пуст"}
    with open(p, encoding="utf-8", errors="replace") as f:
        rows = list(csv.DictReader(f))
    return {"status": "OK" if rows else "NO_DATA", "rows": rows[-1000:], "count": len(rows)}


def latency_sample(root="data", limit=600) -> dict:
    d = os.path.join(root, "raw", "latency_probe")
    files = sorted(os.listdir(d)) if os.path.isdir(d) else []
    rows = []
    for fn in files[-2:]:
        with open(os.path.join(d, fn), encoding="utf-8", errors="replace") as f:
            rows.extend(list(csv.DictReader(f)))
    if not rows:
        return {"status": "NO_LOCAL_RAW", "rows": [],
                "note": "сырые пробы в артефактах; сводка — в research/LATENCY_PROBE.json"}
    out = []
    for r in rows[-limit:]:
        out.append({k: _num(r.get(k)) for k in
                    ("seq", "observation_time", "source_time", "observed_diff_ms",
                     "measured_latency_ms", "req_latency_ms", "book_changed",
                     "measurable", "timestamp_quality", "venue", "instrument")})
    return {"status": "OK", "rows": out, "count": len(out)}


def build() -> dict:
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(os.path.join(OUT, "agg"), exist_ok=True)
    os.makedirs(os.path.join(OUT, "research"), exist_ok=True)

    for fn in os.listdir("data/agg") if os.path.isdir("data/agg") else []:
        if fn.endswith(".json"):
            with open(f"data/agg/{fn}", encoding="utf-8") as a, \
                 open(f"{OUT}/agg/{fn}", "w", encoding="utf-8") as b:
                b.write(a.read())
    for fn in os.listdir("research") if os.path.isdir("research") else []:
        if fn.endswith((".json", ".md")):
            with open(f"research/{fn}", encoding="utf-8") as a, \
                 open(f"{OUT}/research/{fn}", "w", encoding="utf-8") as b:
                b.write(a.read())

    parts = {"dna": dna_sample(), "moves": moves(), "signals": signals(),
             "latency": latency_sample()}
    # бэктест публикуется как есть: режимы и источники внутри уже разделены
    bt = "research/BACKTEST.json"
    if os.path.exists(bt):
        with open(bt, encoding="utf-8") as a, open(f"{OUT}/backtest.json", "w", encoding="utf-8") as b:
            b.write(a.read())
    for name, obj in parts.items():
        # не затираем прежнюю выгрузку пустой: raw уезжает в артефакт
        p = f"{OUT}/{name}.json"
        if obj.get("status") in ("NO_LOCAL_RAW",) and os.path.exists(p):
            try:
                prev = json.load(open(p, encoding="utf-8"))
                if prev.get("rows"):
                    prev["stale"] = True
                    prev["note"] = "показана последняя доступная выгрузка; свежий raw в артефакте"
                    obj = prev
            except Exception:
                pass
        json.dump(obj, open(p, "w", encoding="utf-8"), ensure_ascii=False)

    manifest = {
        "publish_version": PUBLISH_VERSION,
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mode": "SHADOW", "live": False, "venues": ["polymarket"],
        "sources": {k: {"status": v.get("status"), "count": v.get("count", 0),
                        "note": v.get("note", ""), "stale": v.get("stale", False)}
                    for k, v in parts.items()},
    }
    json.dump(manifest, open(f"{OUT}/manifest.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    return manifest


if __name__ == "__main__":
    m = build()
    for k, v in m["sources"].items():
        print(f"  {k:9} {v['status']:14} {v['count']:>5} {v['note'][:50]}")
