"""SHADOW-режим: арена работает на живых данных, ничего не исполняя.

Связывает уже существующие модули: Arena → PortfolioManager → Ledger.
Никакой новой торговой логики здесь нет. Ордера не выставляются никогда:
execution_state всегда SHADOW.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone

from .ledger import Ledger
from .portfolio import PortfolioManager
from .strategy import Arena
from .types import MarketSnapshot, OrderBook

log = logging.getLogger("polylab.shadow")
STATE_FILE = os.getenv("SHADOW_STATE", "data/state/shadow.json")
MODE = "shadow"


def _ob(m: dict) -> OrderBook:
    return OrderBook(bid=m.get("best_bid"), ask=m.get("best_ask"), spread=m.get("spread"),
                     depth_usd=m.get("ask_depth"), imbalance=m.get("imbalance"),
                     levels=tuple())


class ShadowRunner:
    """Прогоняет снимки через арену и пишет решения в журнал."""

    def __init__(self, strategies: list, bankroll: float = 1000.0):
        self.arena = Arena(strategies)
        self.portfolio = PortfolioManager(bankroll)
        self.ledger = Ledger()
        self.stats = {"snapshots": 0, "signals": 0, "accepted": 0, "reduced": 0,
                      "rejected": 0, "conflicts": 0, "resolved": 0, "by_strategy": {},
                      "errors": {}}
        self.load()

    # ── один снимок рынка ──
    def on_market(self, mkt: dict, row: dict, book_up: dict, book_dn: dict,
                  now: datetime) -> list:
        """row — та же строка, что уходит в Market DNA. Данные не дублируются."""
        if row.get("reference_price") is None or row.get("current_price") is None:
            return []
        snap = MarketSnapshot(
            ts=now, asset=mkt["asset"], market_id=str(mkt["market_id"]),
            window_minutes=mkt["window_minutes"], start=mkt["start"], end=mkt["end"],
            elapsed=float(row["elapsed"]), remaining_sec=float(row["time_remaining"]),
            reference_price=float(row["reference_price"]),
            current_price=float(row["current_price"]),
            move=float(row["move"]) if row.get("move") is not None else 0.0,
            up_token=mkt.get("up_token", ""), down_token=mkt.get("down_token", ""),
            up_ask=book_up.get("best_ask"), down_ask=book_dn.get("best_ask"),
            up_book=_ob(book_up), down_book=_ob(book_dn),
            features={"bankroll": self.portfolio.bankroll,
                      "volatility": row.get("volatility"),
                      "acceleration": row.get("acceleration"),
                      "liquidity": row.get("liquidity"),
                      "spread_up": book_up.get("spread"),
                      "imbalance_up": book_up.get("imbalance")},
        )
        self.stats["snapshots"] += 1
        signals = self.arena.collect(snap)
        if self.arena.errors:
            self.stats["errors"] = dict(self.arena.errors)
        if not signals:
            return []

        window_start = mkt["start"].isoformat()
        out = []
        for sig, dec in self.portfolio.decide_batch(signals, window_start):
            self.stats["signals"] += 1
            s = self.stats["by_strategy"].setdefault(
                sig.strategy, {"signals": 0, "accepted": 0, "rejected": 0, "resolved": 0})
            s["signals"] += 1
            key = {"ACCEPT": "accepted", "REDUCE": "reduced",
                   "REJECT": "rejected", "CONFLICT": "conflicts"}.get(dec.action)
            if key:
                self.stats[key] = self.stats.get(key, 0) + 1
            if dec.accepted:
                s["accepted"] += 1
            else:
                s["rejected"] += 1

            bu = book_up if sig.direction == "UP" else book_dn
            row_l = {
                "ts": now.isoformat(), "strategy": sig.strategy,
                "strategy_version": sig.strategy_version, "config_version": sig.config_version,
                "feature_version": snap.feature_version, "mode": MODE,
                "venue": "polymarket", "asset": sig.asset, "market_id": sig.market_id,
                "window": f"{sig.window_minutes}m", "direction": sig.direction,
                "elapsed": round(snap.elapsed, 4), "remaining_sec": round(snap.remaining_sec, 1),
                "reference_price": snap.reference_price, "current_price": snap.current_price,
                "move_pct": round(snap.move * 100, 4), "entry_price": sig.entry_price,
                "bid": bu.get("best_bid"), "ask": bu.get("best_ask"), "spread": bu.get("spread"),
                "depth_usd": bu.get("ask_depth"), "imbalance": bu.get("imbalance"),
                "confidence": sig.confidence, "regime": "",
                "entry_bucket": "", "move_bucket": "", "elapsed_bucket": "",
                "decision": dec.action, "decision_reason": dec.reason, "risk_gate": dec.gate,
                "size_requested": round(sig.size_hint, 2), "size_granted": round(dec.size, 2),
                # SHADOW: ордеров не существует ни при каких условиях
                "execution_state": "SHADOW", "execution_quality": "",
                "filled_shares": "", "remaining_shares": "", "average_fill_price": "",
                "slippage": "", "resolution": "", "hypothetical_pnl": "", "realized_pnl": "",
                "bankroll": round(self.portfolio.bankroll, 2),
                "exposure": round(self.portfolio.exposure(), 2),
                "open_positions": len(self.portfolio.positions),
                "meta": json.dumps({"reason": sig.reason, **sig.meta}, ensure_ascii=False),
            }
            # гипотетический размер: если риск урезал — считаем по запрошенному,
            # иначе «что было бы» перестанет быть сравнимым между сигналами
            row_l["size_requested"] = round(sig.size_hint or dec.size, 2)
            self.ledger.add_pending(
                row_l, mkt["end"] + timedelta(seconds=90),
                {"asset": sig.asset, "direction": sig.direction,
                 "reference_price": snap.reference_price,
                 "market_id": sig.market_id, "strategy": sig.strategy})
            out.append((sig, dec))
        return out

    # ── резолв ──
    def resolve(self, now: datetime, price_at) -> int:
        done = self.ledger.resolve_pending(now, price_at)
        for r in done:
            self.stats["resolved"] += 1
            s = self.stats["by_strategy"].setdefault(
                r.get("strategy", "?"), {"signals": 0, "accepted": 0, "rejected": 0, "resolved": 0})
            s["resolved"] += 1
            # позиция портфеля освобождается; realized не начисляется — это SHADOW
            self.portfolio.release(f"{r.get('strategy')}:{r.get('market_id')}", 0.0)
        return len(done)

    # ── состояние ──
    def save(self) -> None:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ledger": self.ledger.state(), "portfolio": self.portfolio.state(),
                       "arena": self.arena.state(), "stats": self.stats}, f,
                      ensure_ascii=False, default=str)
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, STATE_FILE)

    def load(self) -> None:
        if not os.path.exists(STATE_FILE):
            return
        try:
            d = json.load(open(STATE_FILE, encoding="utf-8"))
        except Exception as e:
            log.warning("не удалось прочитать состояние shadow: %s", e)
            return
        self.ledger.load_state(d.get("ledger") or {})
        self.portfolio.load_state(d.get("portfolio") or {})
        self.arena.load_state(d.get("arena") or {})
        st = d.get("stats") or {}
        for k, v in st.items():
            if k in self.stats and isinstance(self.stats[k], dict):
                self.stats[k].update(v or {})
            elif k in self.stats:
                self.stats[k] = v
