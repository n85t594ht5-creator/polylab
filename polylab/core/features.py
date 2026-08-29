"""Признаки Market DNA.

Правило, нарушение которого делает всё исследование бессмысленным:
функции здесь физически не получают на вход ничего из будущего. Им доступна
только история снимков ДО текущего момента включительно.

Всё, что становится известно позже, живёт в Outcome (см. types.Outcome) и
связывается со снимком по snapshot_id.
"""
from __future__ import annotations

import math
from typing import Optional

FEATURE_VERSION = "1"


def snapshot_id(market_id: str, ts_epoch: float, bucket_sec: int = 15) -> str:
    """Детерминированный ID: один рынок + одно временное ведро = один снимок.

    Благодаря этому повторный запуск коллектора в том же ведре не создаёт дубль.
    """
    return f"{market_id}@{int(ts_epoch // bucket_sec) * bucket_sec}"


def realized_vol(prices: list[float]) -> Optional[float]:
    """Ст. отклонение доходностей по имеющейся истории. Меньше 5 точек — None."""
    if len(prices) < 5:
        return None
    rets = [(prices[i] - prices[i - 1]) / prices[i - 1]
            for i in range(1, len(prices)) if prices[i - 1]]
    if len(rets) < 4:
        return None
    m = sum(rets) / len(rets)
    return round(math.sqrt(sum((r - m) ** 2 for r in rets) / len(rets)), 8)


def acceleration(prices: list[float]) -> Optional[float]:
    """Ускорение: движение за последнюю треть истории против средней. Нужно ≥6 точек."""
    if len(prices) < 6:
        return None
    k = max(2, len(prices) // 3)
    recent = (prices[-1] - prices[-k]) / prices[-k] if prices[-k] else None
    whole = (prices[-1] - prices[0]) / prices[0] if prices[0] else None
    if recent is None or whole is None or whole == 0:
        return None
    return round(recent / whole, 4)


def reversals(prices: list[float], eps: float = 1e-9) -> Optional[int]:
    """Сколько раз направление менялось в имеющейся истории."""
    if len(prices) < 3:
        return None
    signs = [1 if b - a > eps else -1 if a - b > eps else 0
             for a, b in zip(prices, prices[1:])]
    signs = [s for s in signs if s]
    return sum(1 for a, b in zip(signs, signs[1:]) if a != b) if signs else 0


def build_features(hist: list[dict], market: dict, book_up: dict, book_dn: dict) -> dict:
    """Вектор признаков момента. hist — снимки этого окна ДО текущего включительно."""
    prices = [h["current_price"] for h in hist if h.get("current_price")]
    return {
        "volatility": realized_vol(prices),
        "acceleration": acceleration(prices),
        "reversals_so_far": reversals(prices),
        "samples_so_far": len(prices),
        "liquidity": market.get("liquidity"),
        "tick_size": market.get("tick_size"),
        "spread_up": book_up.get("spread"),
        "spread_down": book_dn.get("spread"),
        "imbalance_up": book_up.get("imbalance"),
        "imbalance_down": book_dn.get("imbalance"),
        # объём торгов Polymarket источником не предоставляется — см. DATA_SCHEMA.md
        "market_volume": None,
        "feature_version": FEATURE_VERSION,
    }


def quality(snap_row: dict, stale_ms: Optional[float]) -> tuple:
    """Статус качества записи и список пустых полей. Плохие данные не прячем."""
    critical = ("current_price", "reference_price", "up_ask", "down_ask")
    optional = ("volatility", "acceleration", "spread_up", "imbalance_up",
                "depth_1_up", "book_ts_up")
    missing = [k for k in critical + optional if snap_row.get(k) is None]
    if any(snap_row.get(k) is None for k in ("current_price", "reference_price")):
        return "INVALID", missing
    if stale_ms is not None and stale_ms > 60_000:
        return "STALE", missing
    if any(snap_row.get(k) is None for k in ("up_ask", "down_ask")):
        return "INCOMPLETE", missing
    if missing:
        return "DEGRADED", missing
    return "GOOD", missing
