"""Portfolio Manager и Risk Engine.

Стратегии предлагают — портфель решает. Здесь и только здесь известны общий
банкролл, экспозиция, корреляция направлений и конфликты между стратегиями.

В shadow/paper реальными деньгами не распоряжается: все решения — записи в журнал.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from .types import Decision, Signal

DEFAULT_LIMITS = {
    "max_positions": 6,          # всего открытых позиций
    "max_exposure": 0.20,        # доля банкролла во всех позициях
    "max_stake": 0.05,           # потолок одной ставки
    "max_per_window": 1,         # позиций на одно окно времени (корреляция!)
    "max_same_dir": 3,           # одновременно в одну сторону
    "max_per_strategy": 3,       # чтобы одна стратегия не заняла весь портфель
    "daily_loss_limit": 0.30,    # доля банкролла на начало дня
}


class PortfolioManager:
    """Принимает решение по каждому сигналу: ACCEPT / REDUCE / REJECT / CONFLICT."""

    def __init__(self, bankroll: float, limits: dict | None = None):
        self.bankroll = bankroll
        self.day_start_bankroll = bankroll
        self.limits = {**DEFAULT_LIMITS, **(limits or {})}
        self.positions: dict[str, dict] = {}   # key -> {strategy, asset, side, cost, window_start}
        self.day_pnl = 0.0

    # ── состояние ──
    def exposure(self) -> float:
        return sum(p["cost"] for p in self.positions.values())

    def dir_exposure(self) -> dict:
        out = {"UP": 0.0, "DOWN": 0.0}
        for p in self.positions.values():
            out[p["side"]] = out.get(p["side"], 0.0) + p["cost"]
        return out

    def strategy_exposure(self) -> dict:
        out: dict = defaultdict(float)
        for p in self.positions.values():
            out[p["strategy"]] += p["cost"]
        return dict(out)

    def daily_limit_usd(self) -> float:
        lim = self.limits["daily_loss_limit"]
        base = self.day_start_bankroll or self.bankroll
        return base * lim if lim <= 1 else lim

    # ── решение ──
    def decide(self, sig: Signal, window_start: str = "") -> Decision:
        L = self.limits
        if self.day_pnl <= -self.daily_limit_usd():
            return Decision("REJECT", reason="дневной лимит убытка", gate="DAILY_LOSS_LIMIT")
        if len(self.positions) >= L["max_positions"]:
            return Decision("REJECT", reason="лимит открытых позиций", gate="MAX_POSITIONS")

        # конфликт: другая стратегия уже стоит в противоположную сторону на этом рынке
        opposite = [p for p in self.positions.values()
                    if p.get("market_id") == sig.market_id and p["side"] != sig.direction]
        if opposite:
            who = ", ".join(sorted({p["strategy"] for p in opposite}))
            return Decision("CONFLICT", reason=f"противоположная позиция от {who}", gate="CONFLICT")

        same_window = sum(1 for p in self.positions.values() if p.get("window_start") == window_start)
        if window_start and same_window >= L["max_per_window"]:
            return Decision("REJECT", reason="лимит на одно окно времени", gate="MAX_PER_WINDOW")
        same_dir = sum(1 for p in self.positions.values() if p["side"] == sig.direction)
        if same_dir >= L["max_same_dir"]:
            return Decision("REJECT", reason="лимит одного направления", gate="MAX_SAME_DIR")
        per_strat = sum(1 for p in self.positions.values() if p["strategy"] == sig.strategy)
        if per_strat >= L["max_per_strategy"]:
            return Decision("REJECT", reason="лимит на стратегию", gate="MAX_PER_STRATEGY")

        want = sig.size_hint or self.bankroll * L["max_stake"]
        cap_stake = self.bankroll * L["max_stake"]
        room = self.bankroll * L["max_exposure"] - self.exposure()
        size = min(want, cap_stake, room)
        if size < 1.0:
            return Decision("REJECT", reason="не осталось места по экспозиции", gate="MAX_EXPOSURE")
        if size < want * 0.999:
            return Decision("REDUCE", size=round(size, 2),
                            reason=f"урезано с {want:.2f}$ до {size:.2f}$",
                            gate="MAX_STAKE" if size == cap_stake else "MAX_EXPOSURE")
        return Decision("ACCEPT", size=round(size, 2), reason="в пределах лимитов")

    def decide_batch(self, signals: Iterable[Signal], window_start: str = "") -> list[tuple]:
        """Решения по пачке сигналов одного снимка — учитывая уже принятые в этой же пачке."""
        out = []
        for sig in sorted(signals, key=lambda s: -s.confidence):   # уверенные первыми
            d = self.decide(sig, window_start)
            if d.accepted:
                self.reserve(sig, d, window_start)
            out.append((sig, d))
        return out

    # ── учёт позиций ──
    def reserve(self, sig: Signal, dec: Decision, window_start: str = "") -> str:
        key = f"{sig.strategy}:{sig.market_id}"
        self.positions[key] = {"strategy": sig.strategy, "asset": sig.asset, "side": sig.direction,
                               "cost": dec.size, "window_start": window_start,
                               "market_id": sig.market_id}
        return key

    def release(self, key: str, pnl: float = 0.0) -> None:
        if key in self.positions:
            del self.positions[key]
        self.bankroll += pnl
        self.day_pnl += pnl

    def roll_day(self) -> None:
        self.day_pnl = 0.0
        self.day_start_bankroll = self.bankroll

    def snapshot(self) -> dict:
        return {"bankroll": round(self.bankroll, 2), "exposure": round(self.exposure(), 2),
                "dir_exposure": {k: round(v, 2) for k, v in self.dir_exposure().items()},
                "strategy_exposure": {k: round(v, 2) for k, v in self.strategy_exposure().items()},
                "open_positions": len(self.positions), "day_pnl": round(self.day_pnl, 2),
                "daily_limit_usd": round(self.daily_limit_usd(), 2), "limits": self.limits}

    def state(self) -> dict:
        return {"bankroll": self.bankroll, "day_start_bankroll": self.day_start_bankroll,
                "day_pnl": self.day_pnl, "positions": self.positions}

    def load_state(self, d: dict) -> None:
        if not d:
            return
        self.bankroll = d.get("bankroll", self.bankroll)
        self.day_start_bankroll = d.get("day_start_bankroll", self.bankroll)
        self.day_pnl = d.get("day_pnl", 0.0)
        self.positions = dict(d.get("positions") or {})
