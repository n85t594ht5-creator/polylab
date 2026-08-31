"""Движок бэктеста POLYLAB.

Прогоняет собранные снимки через ТЕ ЖЕ Arena → Portfolio → Ledger, что и SHADOW.
Отдельной торговой логики нет — иначе бэктест проверял бы не то, что работает.

Каждый прогон помечен режимом (BACKTEST / OOS / FORWARD) и источником данных.
Результаты разных режимов не смешиваются никогда.
"""
from __future__ import annotations

import json, math, os, sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from polylab.core.portfolio import PortfolioManager  # noqa: E402
from polylab.core.strategy import Arena  # noqa: E402
from polylab.core.types import MarketSnapshot, OrderBook  # noqa: E402

ENGINE_VERSION = "1"
MIN_SAMPLE = int(os.getenv("MIN_SAMPLE", "100"))       # порог для предварительной оценки
MIN_PER_WINDOW = int(os.getenv("MIN_PER_WINDOW", "50"))


def _f(v):
    if v in ("", None):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _snap(r: dict, bankroll: float) -> MarketSnapshot:
    ob = lambda p: OrderBook(bid=_f(r.get(f"best_bid_{p}")), ask=_f(r.get(f"best_ask_{p}")),
                             spread=_f(r.get(f"spread_{p}")), depth_usd=_f(r.get(f"ask_depth_{p}")),
                             imbalance=_f(r.get(f"imbalance_{p}")))
    return MarketSnapshot(
        ts=datetime.fromisoformat(r["ts"]), asset=r.get("asset", "?"),
        market_id=str(r.get("market_id")), window_minutes=int(str(r.get("window", "0m")).rstrip("m") or 0),
        start=datetime.fromisoformat(r["start"]) if r.get("start") else datetime.fromisoformat(r["ts"]),
        end=datetime.fromisoformat(r["end"]) if r.get("end") else datetime.fromisoformat(r["ts"]),
        elapsed=_f(r.get("elapsed")) or 0.0, remaining_sec=_f(r.get("time_remaining")) or 0.0,
        reference_price=_f(r.get("reference_price")) or 0.0,
        current_price=_f(r.get("current_price")) or 0.0, move=_f(r.get("move")) or 0.0,
        up_ask=_f(r.get("up_ask")), down_ask=_f(r.get("down_ask")),
        up_book=ob("up"), down_book=ob("dn"),
        features={"bankroll": bankroll, "volatility": _f(r.get("volatility")),
                  "acceleration": _f(r.get("acceleration")), "liquidity": _f(r.get("liquidity")),
                  "spread_up": _f(r.get("spread_up")), "imbalance_up": _f(r.get("imbalance_up"))})


def outcome_of(snaps: list) -> str | None:
    """Исход окна по последнему известному снимку.

    ВАЖНО: цена окончания окна берётся из ПОСЛЕДНЕГО снимка этого окна. Если
    последний снимок сделан заметно раньше конца — исход неизвестен, и окно
    исключается, а не додумывается.
    """
    last = snaps[-1]
    rem = _f(last.get("time_remaining"))
    if rem is None or rem > 60:
        return None
    ref, cur = _f(last.get("reference_price")), _f(last.get("current_price"))
    if ref is None or cur is None:
        return None
    return "UP" if cur > ref else "DOWN"


def run(strategies_factory, by_market: dict, mode: str, source: str,
        bankroll: float = 1000.0, stake_pct: float = 0.05) -> dict:
    """Один прогон. Возвращает сделки и метрики. Ничего не пишет на диск."""
    arena = Arena(strategies_factory())
    pf = PortfolioManager(bankroll)
    trades: list[dict] = []
    skipped_unknown = 0
    # Диагностика отсева: без неё «0 сделок» неотличимо от поломки движка.
    stage = {"windows": 0, "outcome_unknown": 0, "snapshots": 0, "snapshot_errors": 0,
             "no_price": 0, "no_signal": 0, "with_signal": 0, "blocked_by_risk": 0, "entered": 0}

    order = sorted(by_market.items(), key=lambda kv: kv[1][0].get("ts") or "")
    for mid, snaps in order:
        stage["windows"] += 1
        res = outcome_of(snaps)
        if res is None:
            skipped_unknown += 1
            stage["outcome_unknown"] += 1
            continue
        entered = False
        for r in snaps:
            if entered:
                break
            stage["snapshots"] += 1
            try:
                snap = _snap(r, pf.bankroll)
            except Exception:
                stage["snapshot_errors"] += 1
                continue
            # Условия входа принадлежат стратегии и здесь НЕ повторяются:
            # дублирование разошлось бы с ней при первом же изменении.
            # Считаем только независимые от стратегии стадии.
            if snap.up_ask is None and snap.down_ask is None:
                stage["no_price"] += 1
            sigs = arena.collect(snap)
            if not sigs:
                stage["no_signal"] += 1
            else:
                stage["with_signal"] += 1
            for sig in sigs:
                dec = pf.decide(sig, snap.start.isoformat())
                if not dec.accepted:
                    stage["blocked_by_risk"] += 1
                    trades.append({"ts": r["ts"], "day": r["ts"][:10], "strategy": sig.strategy,
                                   "strategy_version": sig.strategy_version,
                                   "asset": sig.asset, "window": f"{sig.window_minutes}m",
                                   "direction": sig.direction, "entry": sig.entry_price,
                                   "confidence": sig.confidence, "decision": dec.action,
                                   "gate": dec.gate, "resolution": None, "pnl": None,
                                   "mode": mode, "source": source})
                    continue
                pf.reserve(sig, dec, snap.start.isoformat())
                won = sig.direction == res
                size = dec.size or bankroll * stake_pct
                pnl = size * (1 - sig.entry_price) / sig.entry_price if won else -size
                trades.append({"ts": r["ts"], "day": r["ts"][:10], "strategy": sig.strategy,
                               "strategy_version": sig.strategy_version,
                               "asset": sig.asset, "window": f"{sig.window_minutes}m",
                               "direction": sig.direction, "entry": sig.entry_price,
                               "confidence": sig.confidence, "decision": dec.action,
                               "gate": dec.gate, "resolution": "WIN" if won else "LOSS",
                               "size": round(size, 2), "pnl": round(pnl, 2),
                               "mode": mode, "source": source})
                pf.release(f"{sig.strategy}:{sig.market_id}", pnl)
                stage["entered"] += 1
                entered = True
                break
    return {"mode": mode, "source": source, "engine_version": ENGINE_VERSION,
            "trades": trades, "windows_seen": len(by_market),
            "windows_outcome_unknown": skipped_unknown, "funnel": stage,
            "metrics": metrics(trades), "arena_errors": dict(arena.errors)}


def metrics(trades: list) -> dict:
    """Метрики с обязательной проверкой на концентрацию и размер выборки."""
    closed = [t for t in trades if t.get("resolution")]
    n = len(closed)
    base = {"signals": len(trades), "closed": n, "blocked": len(trades) - n}
    if not n:
        return {**base, "status": "NO_DATA", "note": "закрытых сделок нет"}

    wins = [t for t in closed if t["resolution"] == "WIN"]
    gp = sum(t["pnl"] for t in wins)
    gl = -sum(t["pnl"] for t in closed if t["resolution"] == "LOSS")
    pnl = gp - gl
    by_day: dict = defaultdict(float)
    for t in closed:
        by_day[t["day"]] += t["pnl"]
    days = sorted(by_day.items(), key=lambda kv: -abs(kv[1]))
    best_share = round(abs(days[0][1]) / abs(pnl) * 100, 1) if pnl else None
    without_best = round(pnl - days[0][1], 2) if days else None
    top3 = round(sum(v for _, v in days[:3]), 2)

    eq, peak, dd = 0.0, 0.0, 0.0
    for t in closed:
        eq += t["pnl"]; peak = max(peak, eq); dd = max(dd, peak - eq)

    by_win: dict = defaultdict(lambda: {"n": 0, "w": 0, "pnl": 0.0})
    for t in closed:
        b = by_win[t["window"]]; b["n"] += 1; b["w"] += t["resolution"] == "WIN"; b["pnl"] += t["pnl"]

    enough = n >= MIN_SAMPLE and all(v["n"] >= MIN_PER_WINDOW for v in by_win.values())
    concentrated = best_share is not None and best_share > 50
    status = ("INSUFFICIENT_SAMPLE" if not enough else
              "CONCENTRATED" if concentrated else "SUPPORTED")

    return {**base, "status": status,
            "winrate": round(len(wins) / n, 4), "pnl": round(pnl, 2),
            "profit_factor": round(gp / gl, 2) if gl else (99.0 if gp else 0.0),
            "max_drawdown": round(dd, 2), "days": len(by_day),
            "best_day": days[0][0] if days else None,
            "best_day_pnl": round(days[0][1], 2) if days else None,
            "best_day_share_pct": best_share, "pnl_without_best_day": without_best,
            "top3_days_pnl": top3,
            "by_window": {k: {"n": v["n"], "winrate": round(v["w"] / v["n"], 4),
                              "pnl": round(v["pnl"], 2)} for k, v in by_win.items()},
            "sample_note": (f"нужно ≥{MIN_SAMPLE} закрытых сделок и ≥{MIN_PER_WINDOW} на окно"
                            if not enough else ""),
            "concentration_note": ("результат держится на одном дне — это событие, а не edge"
                                   if concentrated else "")}


def verdict(is_m: dict, oos_m: dict, fwd_m: dict | None = None) -> dict:
    """Итоговая оценка. По умолчанию — INCONCLUSIVE."""
    reasons = []
    if is_m.get("status") == "NO_DATA" or oos_m.get("status") == "NO_DATA":
        return {"verdict": "INCONCLUSIVE", "reasons": ["нет закрытых сделок в одной из выборок"]}
    if is_m.get("status") == "INSUFFICIENT_SAMPLE" or oos_m.get("status") == "INSUFFICIENT_SAMPLE":
        reasons.append("выборка меньше порога — вывод невозможен")
    if is_m.get("status") == "CONCENTRATED":
        reasons.append("in-sample держится на одном дне")
    if oos_m.get("status") == "CONCENTRATED":
        reasons.append("out-of-sample держится на одном дне")
    degraded = (oos_m.get("profit_factor", 0) < 1.0 <= is_m.get("profit_factor", 0))
    if degraded:
        reasons.append("на нетронутых данных результат разваливается")
    if reasons:
        return {"verdict": "INCONCLUSIVE" if "выборка" in reasons[0] else "NOT SUPPORTED",
                "reasons": reasons}
    if fwd_m and fwd_m.get("status") in ("INSUFFICIENT_SAMPLE", "NO_DATA"):
        return {"verdict": "INCONCLUSIVE", "reasons": ["forward-выборки недостаточно"]}
    return {"verdict": "SUPPORTED",
            "reasons": ["выборка достаточна, результат не держится на одном дне, "
                        "out-of-sample не разваливается"]}
