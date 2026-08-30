#!/usr/bin/env python3
"""5D.2 — высокочастотный зонд задержек. НЕЗАВИСИМ от Market DNA collector.

Пишет в отдельный файл, не трогает data/raw/dna, не влияет на агрегаты.
Задача одна: выяснить, измеряема ли задержка вообще при доступной точности.

Что честно измеримо:
  - request latency (наш запрос → ответ) — измеримо прямо
  - polling jitter — измеримо
  - изменение стакана между опросами (по hash) — наблюдаемо
  - разница времён источника и стакана — НАБЛЮДАЕМАЯ, не причинная

Что неизмеримо и так и помечается:
  - задержка короче интервала опроса
  - абсолютная задержка в пределах расхождения часов
"""
from __future__ import annotations

import csv, json, os, statistics, sys, time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from polylab.data import sources as SRC  # noqa: E402

PROBE_VERSION = "1"
INTERVAL = float(os.getenv("PROBE_INTERVAL", "0.5"))     # высокая частота
RUN_SEC = int(os.getenv("PROBE_RUN_SEC", "120"))
ASSET = os.getenv("PROBE_ASSET", "BTC")
OUT = "data/raw/latency_probe"

# venue/instrument вынесены отдельно: ядро POLYLAB не должно быть привязано
# к Polymarket — те же поля подойдут для другой площадки через свой адаптер.
FIELDS = ["seq", "venue", "instrument", "market_id", "event_type",
          "observation_time", "source_time", "source_trade_id", "source_price",
          "reaction_time", "book_ts", "book_hash", "book_changed", "best_ask", "best_bid",
          "req_latency_ms", "interval_ms", "probe_interval_ms",
          "observed_diff_ms", "measured_latency_ms", "timestamp_quality",
          "measurable", "causality", "clock_note", "probe_version"]


def parse_ts(s: str):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp() * 1000
    except Exception:
        return None


def run() -> dict:
    os.makedirs(OUT, exist_ok=True)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = os.path.join(OUT, f"{day}.csv")
    markets = SRC.list_updown_markets([ASSET], [5, 15, 60])
    if not markets:
        return {"status": "NO_MARKETS", "note": "активных окон не найдено"}
    token = markets[0]["up_token"]

    rows, req_lat, intervals, changes, diffs = [], [], [], 0, []
    prev_hash, prev_t = None, None
    seq = 0
    t_end = time.time() + RUN_SEC
    while time.time() < t_end:
        t0 = time.time()
        tk = SRC.ticker(ASSET)
        book = SRC.fetch_book(token)
        obs = datetime.now(timezone.utc)
        rl = (time.time() - t0) * 1000
        req_lat.append(rl)
        if prev_t is not None:
            intervals.append((t0 - prev_t) * 1000)
        prev_t = t0

        bm = SRC.book_metrics(book)
        bh = bm.get("book_hash")
        changed = int(prev_hash is not None and bh != prev_hash)
        changes += changed
        prev_hash = bh

        st = parse_ts((tk or {}).get("time"))
        bts = bm.get("book_ts")
        diff = (bts - st) if (st and bts) else None
        if diff is not None:
            diffs.append(diff)
        poll_ms = INTERVAL * 1000
        measurable = "NOT_MEASURABLE"
        if diff is not None:
            if abs(diff) >= 3 * poll_ms and abs(diff) > 1000:
                measurable = "MEASURABLE"
            elif abs(diff) > 1000:
                measurable = "BORDERLINE"

        # reaction_time: момент, когда стакан впервые изменился после нового тика источника
        reaction = obs.isoformat() if changed else None
        measured = None
        if changed and st is not None:
            measured = round(obs.timestamp() * 1000 - st)     # наблюдаемая, не причинная
        tq = "MS" if ((tk or {}).get("time") or "").count(".") and bts else \
             "MIXED" if bts or (tk or {}).get("time") else "UNAVAILABLE"

        seq += 1
        rows.append({
            "seq": seq, "venue": "polymarket", "instrument": ASSET,
            "market_id": markets[0]["market_id"], "event_type": "book_state",
            "reaction_time": reaction, "measured_latency_ms": measured,
            "timestamp_quality": tq, "probe_interval_ms": round(INTERVAL * 1000),
            "causality": "CAUSALITY NOT ESTABLISHED",
            "observation_time": obs.isoformat(),
            "source_time": (tk or {}).get("time"), "source_trade_id": (tk or {}).get("trade_id"),
            "source_price": (tk or {}).get("price"), "book_ts": bts, "book_hash": bh,
            "book_changed": changed, "best_ask": bm.get("best_ask"), "best_bid": bm.get("best_bid"),
            "req_latency_ms": round(rl, 1), "interval_ms": round(intervals[-1], 1) if intervals else None,
            "observed_diff_ms": None if diff is None else round(diff),
            "clock_note": "разница часов точнее 1 с не измеряется (заголовок Date без долей)",
            "measurable": measurable, "probe_version": PROBE_VERSION,
        })
        time.sleep(max(0.0, INTERVAL - (time.time() - t0)))

    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in FIELDS})

    def pct(v, p):
        return round(sorted(v)[min(len(v) - 1, int(len(v) * p))], 1) if v else None

    ordered = all(rows[i]["observation_time"] <= rows[i + 1]["observation_time"] for i in range(len(rows) - 1))
    meas = [r["measurable"] for r in rows]
    return {
        "status": "OK", "probe_version": PROBE_VERSION, "asset": ASSET, "samples": len(rows),
        "interval_target_ms": INTERVAL * 1000,
        "interval_actual": {"median": pct(intervals, .5), "p90": pct(intervals, .9), "max": pct(intervals, 1),
                            "jitter_stdev": round(statistics.pstdev(intervals), 1) if len(intervals) > 1 else None},
        "request_latency_ms": {"median": pct(req_lat, .5), "p90": pct(req_lat, .9),
                               "p95": pct(req_lat, .95), "p99": pct(req_lat, .99), "max": pct(req_lat, 1)},
        "book_changes": changes, "book_change_rate": round(changes / max(len(rows) - 1, 1), 3),
        "observed_diff_ms": {"n": len(diffs), "median": pct(diffs, .5),
                             "p90": pct(diffs, .9), "min": min(diffs) if diffs else None,
                             "max": max(diffs) if diffs else None},
        "measurable_counts": {k: meas.count(k) for k in set(meas)},
        "event_ordering_ok": ordered,
        "min_measurable_latency_ms": round(INTERVAL * 1000 + (pct(req_lat, .9) or 0)),
        "verdict": ("INCONCLUSIVE" if meas.count("MEASURABLE") == 0 else "SUPPORTED"),
        "venue": "polymarket", "instrument": ASSET,
        "relationship": "OBSERVED TIMING DIFFERENCE — CAUSALITY NOT ESTABLISHED",
        "timestamp_quality": {"source": "миллисекунды (Coinbase ticker.time)",
                              "market": "миллисекунды (CLOB book.timestamp)",
                              "clock_skew": "точнее 1 с не измеряется"},
        "limits": [
            "задержка короче интервала опроса неизмерима принципиально",
            "расхождение часов точнее 1 с не измеряется — абсолютные значения <1 с не факт",
            "наблюдаемая разница времён ≠ причинная задержка",
        ],
    }


if __name__ == "__main__":
    rep = run()
    os.makedirs("research", exist_ok=True)
    json.dump(rep, open("research/LATENCY_PROBE.json", "w"), ensure_ascii=False, indent=1)
    print(json.dumps(rep, ensure_ascii=False, indent=1)[:1400])
