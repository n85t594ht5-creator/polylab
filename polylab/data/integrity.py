#!/usr/bin/env python3
"""PHASE 10 — целостность накопления raw.

Разделяет два разных факта, которые до сих пор смешивались:
  1) агрегат не уменьшился  — защита долгосрочных данных (5C);
  2) raw неполон            — потеря сырых снимков.

Второе больше не маскируется первым: если raw меньше агрегата, это видно
явно, попадает в отчёт и в панель.
"""
from __future__ import annotations

import json, os, sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from polylab.data.store import read_gz_rows  # noqa: E402

OUT = "research/RAW_INTEGRITY.json"


def raw_rows(day: str, root: str = "data") -> tuple:
    p = os.path.join(root, "raw", "dna", f"{day}.csv.gz")
    if not os.path.exists(p):
        return 0, False, False
    rows, truncated = read_gz_rows(p)
    return len(rows), truncated, True


def agg_snapshots(day: str, root: str = "data") -> int | None:
    p = os.path.join(root, "agg", f"{day}.json")
    if not os.path.exists(p):
        return None
    try:
        return json.load(open(p, encoding="utf-8")).get("snapshots")
    except Exception:
        return None


def check(days: list[str], root: str = "data", phase: str = "after") -> dict:
    per_day, issues = {}, []
    for day in days:
        n_raw, truncated, present = raw_rows(day, root)
        n_agg = agg_snapshots(day, root)
        d = {"raw_rows": n_raw, "raw_present": present, "raw_truncated": truncated,
             "aggregate_snapshots": n_agg}
        if n_agg is None:
            d["status"] = "NO_AGGREGATE"
        elif not present:
            d["status"] = "RAW_MISSING"
            issues.append(f"{day}: raw отсутствует, агрегат знает о {n_agg} снимках")
        elif n_raw < n_agg:
            d["status"] = "RAW_INCOMPLETE"
            d["lost"] = n_agg - n_raw
            d["completeness"] = round(n_raw / n_agg, 4) if n_agg else None
            issues.append(f"{day}: в raw {n_raw} из {n_agg} снимков — потеряно {n_agg - n_raw}")
        else:
            d["status"] = "OK"
            d["completeness"] = 1.0
        per_day[day] = d
    tot_raw = sum(v["raw_rows"] for v in per_day.values())
    tot_agg = sum(v["aggregate_snapshots"] or 0 for v in per_day.values())
    return {"checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "phase": phase, "days": per_day,
            "total_raw_rows": tot_raw, "total_aggregate_snapshots": tot_agg,
            "completeness": round(tot_raw / tot_agg, 4) if tot_agg else None,
            "issues": issues,
            "verdict": "OK" if not issues else "RAW_LOSS"}


def main() -> None:
    phase = sys.argv[1] if len(sys.argv) > 1 else "after"
    root = "data"
    days = sorted({f[:-5] for f in os.listdir(os.path.join(root, "agg"))
                   if f.endswith(".json") and f != "index.json"}) if os.path.isdir(os.path.join(root, "agg")) else []
    d = os.path.join(root, "raw", "dna")
    days = sorted(set(days) | ({f[:-7] for f in os.listdir(d) if f.endswith(".csv.gz")} if os.path.isdir(d) else set()))
    rep = check(days, root, phase)

    prev = {}
    if os.path.exists(OUT):
        try:
            prev = json.load(open(OUT, encoding="utf-8"))
        except Exception:
            pass
    hist = (prev.get("history") or [])[-40:]
    hist.append({"at": rep["checked_at"], "phase": phase, "completeness": rep["completeness"],
                 "verdict": rep["verdict"], "total_raw": rep["total_raw_rows"],
                 "total_agg": rep["total_aggregate_snapshots"]})
    rep["history"] = hist
    os.makedirs("research", exist_ok=True)
    json.dump(rep, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print(f"[{phase}] полнота raw: {rep['completeness']} · raw {rep['total_raw_rows']} / "
          f"агрегат {rep['total_aggregate_snapshots']} · {rep['verdict']}")
    for i in rep["issues"]:
        print("  ПОТЕРЯ:", i)
    # Перед сбором потеря — повод остановиться: дописывать в обрубленный файл
    # значит закрепить утрату навсегда.
    if phase == "before" and rep["verdict"] == "RAW_LOSS" and os.getenv("STRICT_RAW", "1") == "1":
        print("::error::raw неполон до начала сбора — дозапись закрепила бы потерю")
        sys.exit(2)


if __name__ == "__main__":
    main()
