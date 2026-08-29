#!/usr/bin/env python3
"""Зонд источников данных POLYLAB.

Отвечает на вопрос: какие поля реально отдают API, с какой точностью времени
и какая глубина стакана доступна. Ничего не предполагает — только измеряет.
Результат: research/DATA_AVAILABILITY.json

Запускается в GitHub Actions, потому что из песочницы разработки сеть к
Polymarket/Coinbase закрыта.
"""
from __future__ import annotations

import json
import os
import statistics
import time
from datetime import datetime, timedelta, timezone

import requests

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
COINBASE = "https://api.exchange.coinbase.com"
S = requests.Session()
S.headers["User-Agent"] = "polylab-probe/0.1"

out: dict = {"probed_at": datetime.now(timezone.utc).isoformat(), "sources": {}}


def get(url, **params):
    t0 = time.time()
    r = S.get(url, params=params, timeout=20)
    dt = (time.time() - t0) * 1000
    r.raise_for_status()
    return r.json(), round(dt, 1), dict(r.headers)


def probe(name, fn):
    try:
        out["sources"][name] = fn()
        out["sources"][name]["status"] = "OK"
    except Exception as e:
        out["sources"][name] = {"status": "FAIL", "error": str(e)[:200]}
    print(f"  {name}: {out['sources'][name]['status']}")


def p_gamma():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    j, ms, hdr = get(f"{GAMMA}/markets", closed="false", active="true", limit=50,
                     order="endDate", ascending="true", end_date_min=now)
    updown = [m for m in j if "-updown-" in (m.get("slug") or "")]
    sample = updown[0] if updown else (j[0] if j else {})
    return {"latency_ms": ms, "n_markets": len(j), "n_updown": len(updown),
            "fields": sorted(sample.keys()),
            "has_volume": any(k in sample for k in ("volume", "volumeNum", "volume24hr")),
            "has_liquidity": any(k in sample for k in ("liquidity", "liquidityNum")),
            "server_date_header": hdr.get("Date"),
            "sample_slug": sample.get("slug"),
            "sample_volume": {k: sample.get(k) for k in
                              ("volume", "volumeNum", "volume24hr", "liquidity", "liquidityNum")
                              if k in sample}}


def p_book():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    j, _, _ = get(f"{GAMMA}/markets", closed="false", active="true", limit=50,
                  order="endDate", ascending="true", end_date_min=now)
    updown = [m for m in j if "-updown-" in (m.get("slug") or "")]
    if not updown:
        raise RuntimeError("не найдено активных updown-рынков")
    tok = json.loads(updown[0]["clobTokenIds"])[0]
    b, ms, hdr = get(f"{CLOB}/book", token_id=tok)
    asks, bids = b.get("asks") or [], b.get("bids") or []
    lvl = lambda side: [(float(x["price"]), float(x["size"])) for x in side]
    A, B = sorted(lvl(asks)), sorted(lvl(bids), reverse=True)
    return {"latency_ms": ms, "top_level_fields": sorted((asks[0] if asks else {}).keys()),
            "book_fields": sorted(b.keys()),
            "has_timestamp": any(k in b for k in ("timestamp", "time", "ts")),
            "timestamp_value": b.get("timestamp") or b.get("time"),
            "n_ask_levels": len(A), "n_bid_levels": len(B),
            "best_ask": A[0][0] if A else None, "best_bid": B[0][0] if B else None,
            "spread": round(A[0][0] - B[0][0], 4) if A and B else None,
            "ask_depth_usd": round(sum(p * s for p, s in A), 2),
            "bid_depth_usd": round(sum(p * s for p, s in B), 2),
            "server_date_header": hdr.get("Date"), "sample_market": updown[0].get("slug")}


def p_clob_price():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    j, _, _ = get(f"{GAMMA}/markets", closed="false", active="true", limit=50,
                  order="endDate", ascending="true", end_date_min=now)
    updown = [m for m in j if "-updown-" in (m.get("slug") or "")]
    tok = json.loads(updown[0]["clobTokenIds"])[0]
    p, ms, _ = get(f"{CLOB}/price", token_id=tok, side="BUY")
    t, _, _ = get(f"{CLOB}/time")
    return {"latency_ms": ms, "fields": sorted(p.keys()), "value": p,
            "server_time_endpoint": str(t)[:40],
            "server_time_resolution": "секунды" if str(t).isdigit() else "неизвестно"}


def p_coinbase_ticker():
    j, ms, hdr = get(f"{COINBASE}/products/BTC-USD/ticker")
    t = j.get("time", "")
    frac = t.split(".")[1][:-1] if "." in t else ""
    return {"latency_ms": ms, "fields": sorted(j.keys()),
            "time_value": t, "time_fraction_digits": len(frac),
            "resolution": ("миллисекунды" if len(frac) >= 3 else
                           "секунды" if t else "нет времени"),
            "has_volume": "volume" in j, "has_bid_ask": "bid" in j and "ask" in j,
            "server_date_header": hdr.get("Date")}


def p_coinbase_candles():
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=5)
    j, ms, _ = get(f"{COINBASE}/products/BTC-USD/candles", granularity=60,
                   start=start.strftime("%Y-%m-%dT%H:%M:%SZ"), end=end.strftime("%Y-%m-%dT%H:%M:%SZ"))
    return {"latency_ms": ms, "n": len(j), "row_len": len(j[0]) if j else 0,
            "row_format": "[time, low, high, open, close, volume]",
            "granularity_sec": 60, "sample": j[0] if j else None,
            "min_granularity_supported": [60, 300, 900, 3600]}


def p_clock_skew():
    """Насколько наши часы расходятся с сервером — важно для latency-исследования."""
    _, _, hdr = get(f"{COINBASE}/products/BTC-USD/ticker")
    srv = hdr.get("Date")
    if not srv:
        return {"skew_ms": None, "note": "сервер не вернул Date"}
    from email.utils import parsedate_to_datetime
    d = parsedate_to_datetime(srv)
    skew = (datetime.now(timezone.utc) - d).total_seconds() * 1000
    return {"server_date": srv, "skew_ms": round(skew, 1),
            "header_resolution": "секунды (заголовок Date не даёт долей)"}


def p_poll_jitter():
    """Реальный разброс интервала опроса — потолок точности любых измерений."""
    ts = []
    for _ in range(8):
        t0 = time.time()
        try:
            get(f"{COINBASE}/products/BTC-USD/ticker")
        except Exception:
            pass
        ts.append((time.time() - t0) * 1000)
        time.sleep(0.5)
    return {"n": len(ts), "median_ms": round(statistics.median(ts), 1),
            "min_ms": round(min(ts), 1), "max_ms": round(max(ts), 1),
            "stdev_ms": round(statistics.pstdev(ts), 1)}


print("зондирую источники…")
for n, f in (("gamma_markets", p_gamma), ("clob_book", p_book), ("clob_price", p_clob_price),
             ("coinbase_ticker", p_coinbase_ticker), ("coinbase_candles", p_coinbase_candles),
             ("clock_skew", p_clock_skew), ("poll_jitter", p_poll_jitter)):
    probe(n, f)

os.makedirs("research", exist_ok=True)
json.dump(out, open("research/DATA_AVAILABILITY.json", "w"), ensure_ascii=False, indent=1)
print("\nсохранено: research/DATA_AVAILABILITY.json")
print(json.dumps(out, ensure_ascii=False, indent=1)[:2000])
