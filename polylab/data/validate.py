#!/usr/bin/env python3
"""5D.1 — валидация длинной серии.

Проверяет согласованность реальных собранных данных. Ничего не чинит и не
подставляет: находит расхождения и называет их. Источник данных помечается
REAL / REPLAY / SYNTHETIC и никогда не смешивается.
"""
from __future__ import annotations

import csv, gzip, json, os, sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from polylab.data.store import Store, read_gz_rows, DNA_FIELDS  # noqa: E402
from polylab.data import aggregate as AGG  # noqa: E402

VALIDATOR_VERSION = "1"


def validate(root: str = "data", source: str = "REAL") -> dict:
    s = Store(root)
    agg_dir = os.path.join(root, "agg")
    days = sorted(f[:-5] for f in os.listdir(agg_dir)
                  if f.endswith(".json") and f != "index.json") if os.path.isdir(agg_dir) else []

    out = {"validator_version": VALIDATOR_VERSION, "source": source,
           "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "days": len(days), "issues": [], "per_day": {}, "totals": {}}
    if not days:
        out["issues"].append({"kind": "NO_DATA", "detail": "агрегатов нет"})
        return out

    tot = {"snapshots": 0, "valid": 0, "quality": defaultdict(int), "windows": defaultdict(int),
           "assets": defaultdict(int), "markets": set(), "duplicate_ids": 0,
           "raw_present_days": 0, "truncated_days": 0, "incomplete_rebuild_days": 0}
    seen_ids: set[str] = set()
    feature_seen: dict = defaultdict(int)

    for day in days:
        a = json.load(open(os.path.join(agg_dir, f"{day}.json"), encoding="utf-8"))
        d = {"snapshots": a.get("snapshots", 0), "valid": a.get("valid", 0),
             "quality": a.get("quality", {}), "raw_present": a.get("raw_present", False),
             "raw_truncated": a.get("raw_truncated", False),
             "raw_incomplete_on_rebuild": a.get("raw_incomplete_on_rebuild", False)}

        # согласованность самого агрегата
        qsum = sum((a.get("quality") or {}).values())
        if qsum != a.get("snapshots", 0):
            out["issues"].append({"kind": "QUALITY_SUM_MISMATCH", "day": day,
                                  "detail": f"сумма качества {qsum} ≠ снимков {a.get('snapshots')}"})
        for cut, cd in (a.get("cuts") or {}).items():
            csum = sum(v.get("n", 0) for v in cd.values())
            # бакеты могут не покрывать записи с None — это законно, но фиксируем
            if csum > a.get("snapshots", 0):
                out["issues"].append({"kind": "CUT_OVERCOUNT", "day": day, "cut": cut,
                                      "detail": f"{csum} > {a.get('snapshots')}"})
            elif csum < a.get("snapshots", 0):
                d.setdefault("cut_gaps", {})[cut] = a.get("snapshots", 0) - csum
        invalid = (a.get("quality") or {}).get("INVALID", 0)
        if a.get("snapshots", 0) - invalid != a.get("valid", 0):
            out["issues"].append({"kind": "VALID_MISMATCH", "day": day,
                                  "detail": f"valid={a.get('valid')} при snapshots-INVALID="
                                            f"{a.get('snapshots',0)-invalid}"})

        # сверка raw → aggregate, если raw ещё локально
        raw_p = os.path.join(root, "raw", "dna", f"{day}.csv.gz")
        if os.path.exists(raw_p):
            rows, truncated = read_gz_rows(raw_p)
            d["raw_rows"] = len(rows)
            d["raw_truncated_now"] = truncated
            if len(rows) != a.get("snapshots", 0) and not a.get("raw_incomplete_on_rebuild"):
                out["issues"].append({"kind": "RAW_AGG_MISMATCH", "day": day,
                                      "detail": f"raw {len(rows)} ≠ агрегат {a.get('snapshots')}"})
            ids = [r.get("snapshot_id") for r in rows if r.get("snapshot_id")]
            dup_in_day = len(ids) - len(set(ids))
            if dup_in_day:
                out["issues"].append({"kind": "DUPLICATE_IDS", "day": day, "detail": str(dup_in_day)})
            cross = seen_ids & set(ids)
            if cross:
                out["issues"].append({"kind": "DUPLICATE_IDS_ACROSS_DAYS", "day": day,
                                      "detail": f"{len(cross)} пересечений"})
            tot["duplicate_ids"] += dup_in_day + len(cross)
            seen_ids |= set(ids)

            # порядок времени внутри суток
            ts = [r.get("ts") for r in rows if r.get("ts")]
            if ts != sorted(ts):
                out["issues"].append({"kind": "TS_NOT_MONOTONIC", "day": day,
                                      "detail": "ts не монотонны (несколько рынков в одном проходе — ожидаемо)"})
            # утечка будущего в raw
            leaked = [f for f in ("resolution", "final_direction", "max_continuation")
                      if f in (rows[0] if rows else {})]
            if leaked:
                out["issues"].append({"kind": "LEAKAGE", "day": day, "detail": str(leaked)})
            tot["markets"] |= {r.get("market_id") for r in rows if r.get("market_id")}

        for k, v in (a.get("quality") or {}).items():
            tot["quality"][k] += v
        for k, v in ((a.get("cuts") or {}).get("window") or {}).items():
            tot["windows"][k] += v.get("n", 0)
        for k, v in ((a.get("cuts") or {}).get("asset") or {}).items():
            tot["assets"][k] += v.get("n", 0)
        for k, v in (a.get("feature_availability") or {}).items():
            feature_seen[k] += v
        tot["snapshots"] += a.get("snapshots", 0)
        tot["valid"] += a.get("valid", 0)
        tot["raw_present_days"] += bool(a.get("raw_present"))
        tot["truncated_days"] += bool(a.get("raw_truncated"))
        tot["incomplete_rebuild_days"] += bool(a.get("raw_incomplete_on_rebuild"))
        out["per_day"][day] = d

    # ожидаемые окна: отмечаем отсутствие, но НЕ утверждаем, что их нет
    for w in ("5m", "15m", "60m"):
        if w not in tot["windows"]:
            out["issues"].append({"kind": "WINDOW_NOT_OBSERVED", "detail": w,
                                  "note": "не наблюдалось; это не утверждение об отсутствии"})

    n = tot["snapshots"] or 1
    out["totals"] = {
        "snapshots": tot["snapshots"], "valid": tot["valid"],
        "valid_share": round(tot["valid"] / n, 4),
        "quality": dict(tot["quality"]), "windows": dict(tot["windows"]),
        "assets": dict(tot["assets"]), "unique_markets": len(tot["markets"]),
        "duplicate_ids": tot["duplicate_ids"],
        "raw_present_days": tot["raw_present_days"], "truncated_days": tot["truncated_days"],
        "incomplete_rebuild_days": tot["incomplete_rebuild_days"],
        "feature_availability_share": {k: round(v / n, 4) for k, v in sorted(feature_seen.items())},
    }
    out["verdict"] = "OK" if not [i for i in out["issues"]
                                  if i["kind"] not in ("WINDOW_NOT_OBSERVED", "TS_NOT_MONOTONIC")] else "ISSUES"
    return out


def rebuild_check(day: str, root: str = "data") -> dict:
    """Детерминированность: пересборка агрегата даёт тот же результат."""
    p = os.path.join(root, "agg", f"{day}.json")
    if not os.path.exists(p):
        return {"day": day, "status": "NO_AGGREGATE"}
    before = json.load(open(p, encoding="utf-8"))
    raw_p = os.path.join(root, "raw", "dna", f"{day}.csv.gz")
    if not os.path.exists(raw_p):
        # raw живёт в артефакте Actions и локально отсутствует — сравнивать не с чем.
        # Это ожидаемое состояние архитектуры 5C, а не расхождение данных.
        return {"day": day, "status": "RAW_NOT_LOCAL", "stored_snapshots": before.get("snapshots", 0),
                "note": "raw в артефакте; сохранённый агрегат — источник истины"}
    rebuilt = AGG.build(day, root)
    keys = ("snapshots", "valid", "quality", "cuts", "feature_availability", "missing_fields")
    same = all(before.get(k) == rebuilt.get(k) for k in keys)
    if not same and before.get("raw_incomplete_on_rebuild"):
        return {"day": day, "status": "RAW_GONE",
                "note": "raw ушёл в артефакт; сохранённый агрегат защищён от потери"}
    return {"day": day, "status": "DETERMINISTIC" if same else "MISMATCH",
            "diff": [k for k in keys if before.get(k) != rebuilt.get(k)] if not same else []}


if __name__ == "__main__":
    src = os.getenv("DATA_SOURCE", "REAL")
    rep = validate(source=src)
    days = sorted(rep["per_day"])
    rep["rebuild"] = [rebuild_check(d) for d in days[-3:]]
    os.makedirs("research", exist_ok=True)
    json.dump(rep, open("research/LONG_SERIES.json", "w"), ensure_ascii=False, indent=1)
    print(f"источник {rep['source']} · дней {rep['days']} · снимков {rep['totals'].get('snapshots')} "
          f"· валидных {rep['totals'].get('valid')} · вердикт {rep['verdict']}")
    for i in rep["issues"][:10]:
        print("  ", i)
    for r in rep["rebuild"]:
        print("  пересборка:", r)
