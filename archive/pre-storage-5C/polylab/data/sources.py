"""Слой доступа к источникам. Ничего не интерпретирует — только достаёт и меряет.

Все поля соответствуют зонду 5A. Полей «по памяти» здесь нет.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

COLLECTOR_VERSION = "0.1.0"
SCHEMA_VERSION = "1"

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
COINBASE = "https://api.exchange.coinbase.com"
CB_PRODUCT = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD",
              "XRP": "XRP-USD", "DOGE": "DOGE-USD"}
SLUG_RE = re.compile(r"^([a-z]+)-updown-(\d+)(m|h)-(\d+)$")

_S = requests.Session()
_S.headers["User-Agent"] = f"polylab/{COLLECTOR_VERSION}"


class Stats:
    """Счётчики обращений — попадают в отчёт о прогоне."""
    def __init__(self):
        self.requests = 0
        self.failures = 0
        self.retries = 0
        self.latency_ms: list[float] = []

    def as_dict(self) -> dict:
        lat = sorted(self.latency_ms)
        med = lat[len(lat) // 2] if lat else None
        return {"requests": self.requests, "failures": self.failures, "retries": self.retries,
                "latency_median_ms": round(med, 1) if med else None,
                "latency_max_ms": round(max(lat), 1) if lat else None}


STATS = Stats()
_last_call = [0.0]
MIN_INTERVAL = 0.05          # не чаще 20 запросов/сек суммарно


def get(url: str, tries: int = 3, timeout: int = 12, **params):
    """Запрос с паузой между вызовами, повтором и экспоненциальной задержкой."""
    delay = 0.6
    for attempt in range(tries):
        gap = time.time() - _last_call[0]
        if gap < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - gap)
        t0 = time.time()
        try:
            r = _S.get(url, params=params, timeout=timeout)
            _last_call[0] = time.time()
            STATS.requests += 1
            STATS.latency_ms.append((time.time() - t0) * 1000)
            if r.status_code == 429:
                raise requests.HTTPError("429 rate limited")
            r.raise_for_status()
            return r.json()
        except Exception:
            _last_call[0] = time.time()
            if attempt == tries - 1:
                STATS.failures += 1
                return None
            STATS.retries += 1
            time.sleep(delay)
            delay *= 2
    return None


def now() -> datetime:
    return datetime.now(timezone.utc)


# ── Polymarket ──

def list_updown_markets(assets: list[str], windows: list[int]) -> list[dict]:
    """Активные окна Up/Down. Слаг несёт время старта — берём его оттуда, не гадаем."""
    j = get(f"{GAMMA}/markets", closed="false", active="true", limit=200,
            order="endDate", ascending="true",
            end_date_min=now().strftime("%Y-%m-%dT%H:%M:%SZ"))
    out = []
    for m in j or []:
        mt = SLUG_RE.match((m.get("slug") or "").lower())
        if not mt:
            continue
        asset = mt.group(1).upper()
        minutes = int(mt.group(2)) * (60 if mt.group(3) == "h" else 1)
        if asset not in assets or minutes not in windows:
            continue
        try:
            tokens = json.loads(m.get("clobTokenIds") or "[]")
            outcomes = json.loads(m.get("outcomes") or '["Up","Down"]')
        except Exception:
            continue
        if len(tokens) != 2:
            continue
        up = 0 if outcomes[0].lower().startswith("up") else 1
        start = datetime.fromtimestamp(int(mt.group(4)), tz=timezone.utc)
        out.append({
            "market_id": str(m.get("id")), "slug": m.get("slug"), "asset": asset,
            "window_minutes": minutes, "start": start, "end": start + timedelta(minutes=minutes),
            "up_token": tokens[up], "down_token": tokens[1 - up],
            # подтверждённые зондом поля Gamma (volume отсутствует у источника)
            "liquidity": _f(m.get("liquidityNum")), "tick_size": _f(m.get("orderPriceMinTickSize")),
            "min_order_size": _f(m.get("orderMinSize")),
        })
    return out


def fetch_book(token_id: str) -> Optional[dict]:
    """Стакан + серверное время в мс. Возвращает сырую структуру или None."""
    b = get(f"{CLOB}/book", token_id=token_id)
    if not b or "asks" not in b:
        return None
    return b


def book_metrics(raw: Optional[dict]) -> dict:
    """Derived-метрики стакана. Отсутствующий уровень — None, НЕ ноль."""
    empty = {"best_bid": None, "best_ask": None, "spread": None, "bid_depth": None,
             "ask_depth": None, "imbalance": None, "book_ts": None, "book_hash": None,
             "last_trade_price": None, "n_ask_levels": None, "n_bid_levels": None}
    for n in (1, 2, 5, 10):
        empty[f"depth_{n}"] = None
    if not raw:
        return empty
    asks = sorted(((_f(x.get("price")), _f(x.get("size"))) for x in raw.get("asks") or []),
                  key=lambda x: (x[0] is None, x[0]))
    bids = sorted(((_f(x.get("price")), _f(x.get("size"))) for x in raw.get("bids") or []),
                  key=lambda x: (x[0] is None, -(x[0] or 0)))
    asks = [(p, s) for p, s in asks if p is not None and s is not None]
    bids = [(p, s) for p, s in bids if p is not None and s is not None]
    m = dict(empty)
    m["book_ts"] = _f(raw.get("timestamp"))
    m["book_hash"] = raw.get("hash")
    m["last_trade_price"] = _f(raw.get("last_trade_price"))
    m["n_ask_levels"], m["n_bid_levels"] = len(asks), len(bids)
    if asks:
        m["best_ask"] = asks[0][0]
        m["ask_depth"] = round(sum(p * s for p, s in asks), 2)
    if bids:
        m["best_bid"] = bids[0][0]
        m["bid_depth"] = round(sum(p * s for p, s in bids), 2)
    if asks and bids:
        m["spread"] = round(asks[0][0] - bids[0][0], 4)
        tot = m["ask_depth"] + m["bid_depth"]
        m["imbalance"] = round((m["bid_depth"] - m["ask_depth"]) / tot, 4) if tot else None
    # глубина по N уровням: если уровней меньше N — значение недоступно, а не ноль
    for n in (1, 2, 5, 10):
        if len(asks) >= n:
            m[f"depth_{n}"] = round(sum(p * s for p, s in asks[:n]), 2)
    return m


# ── Coinbase ──

def ticker(asset: str) -> Optional[dict]:
    """Тик с временем сделки (мс) — источник для latency-исследования."""
    j = get(f"{COINBASE}/products/{CB_PRODUCT[asset]}/ticker")
    if not j or "price" not in j:
        return None
    return {"price": _f(j.get("price")), "bid": _f(j.get("bid")), "ask": _f(j.get("ask")),
            "size": _f(j.get("size")), "volume": _f(j.get("volume")),
            "trade_id": j.get("trade_id"), "time": j.get("time")}


def minute_ref(asset: str, dt: datetime) -> Optional[float]:
    """Опорная цена окна: TWAP минуты старта (среднее open/close), как у Chainlink.

    Замер 5B показал: у Coinbase есть лаг публикации свечей и пропуски минут без
    сделок. Узкий запрос [start, start+2m] для только что стартовавших 5-минутных
    окон возвращал пустой массив в 100% случаев. Поэтому берём широкий диапазон и
    выбираем ведро ТОЧНО по времени старта. Если его нет — возвращаем None
    (запись получит quality=INVALID), но не подставляем соседнюю минуту.
    """
    bucket = int(dt.timestamp()) // 60 * 60
    j = get(f"{COINBASE}/products/{CB_PRODUCT[asset]}/candles", granularity=60,
            start=(dt - timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            end=(dt + timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    if not j:
        return None
    for c in j:
        if int(c[0]) == bucket:
            return round((float(c[3]) + float(c[4])) / 2, 6)
    return None


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
