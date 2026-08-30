#!/usr/bin/env python3
"""5D.3 — наблюдение за существованием 60-минутных окон.

Не утверждает ничего. Просто фиксирует, какие длины окон реально видны в API,
и накапливает наблюдения между прогонами.
"""
from __future__ import annotations

import json, os, sys
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from polylab.data import sources as SRC  # noqa: E402

OUT = "research/WINDOW_OBSERVATION.json"
ASSETS = [a.strip().upper() for a in os.getenv("ASSETS", "BTC,ETH,SOL,XRP,DOGE").split(",")]


def observe() -> dict:
    # запрашиваем ВСЕ длины, а не только те, что торгуем
    markets = SRC.list_updown_markets(ASSETS, [1, 5, 15, 30, 60, 120, 240, 1440])
    per_window = Counter(f"{m['window_minutes']}m" for m in markets)
    per_asset_window = Counter(f"{m['asset']}/{m['window_minutes']}m" for m in markets)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    hist = {"observations": [], "windows_ever_seen": {}, "assets_ever_seen": []}
    if os.path.exists(OUT):
        try:
            hist = json.load(open(OUT, encoding="utf-8"))
        except Exception:
            pass
    hist["observations"] = (hist.get("observations") or [])[-200:] + [
        {"at": now, "markets": len(markets), "windows": dict(per_window)}]
    ever = Counter(hist.get("windows_ever_seen") or {})
    ever.update(per_window)
    hist["windows_ever_seen"] = dict(ever)
    hist["assets_ever_seen"] = sorted(set(hist.get("assets_ever_seen") or []) |
                                      {m["asset"] for m in markets})
    hist["last_seen_examples"] = {f"{m['window_minutes']}m": m["slug"] for m in markets}
    # детали для отчёта: какие активы/рынки/времена реально видны
    hist["last_detail"] = [{"asset": m["asset"], "window": f"{m['window_minutes']}m",
                            "market_id": m["market_id"], "slug": m["slug"],
                            "start": m["start"].isoformat(), "end": m["end"].isoformat()}
                           for m in markets[:40]]
    first = (hist.get("observations") or [{}])[0].get("at")
    hist["observation_window"] = {"first": first, "last": now,
                                  "count": len(hist.get("observations") or [])}
    if first:
        try:
            fdt = datetime.fromisoformat(first.replace("Z", "+00:00"))
            hist["observation_span_hours"] = round(
                (datetime.now(timezone.utc) - fdt).total_seconds() / 3600, 2)
        except Exception:
            pass
    per_win_assets = {}
    for m in markets:
        per_win_assets.setdefault(f"{m['window_minutes']}m", set()).add(m["asset"])
    hist["assets_by_window"] = {k: sorted(v) for k, v in per_win_assets.items()}
    hist["total_observations"] = len(hist["observations"])
    span = hist.get("observation_span_hours") or 0
    hist["status_60m"] = ("OBSERVED" if ever.get("60m") else
                          "NOT OBSERVED" if span >= 6 else "INCONCLUSIVE")
    hist["note_60m"] = ("60-минутные окна наблюдались" if ever.get("60m") else
                        f"за {span:.1f} ч наблюдения 60-минутные окна ни разу не встретились; "
                        "вывода об их отсутствии не делаем — только фиксируем факт наблюдения")
    return hist


if __name__ == "__main__":
    h = observe()
    os.makedirs("research", exist_ok=True)
    json.dump(h, open(OUT, "w"), ensure_ascii=False, indent=1)
    print(f"наблюдений: {h['total_observations']} | окна за всё время: {h['windows_ever_seen']}")
    print(f"активы: {h['assets_ever_seen']}")
    print(f"60m: {h['status_60m']} — {h['note_60m']}")
