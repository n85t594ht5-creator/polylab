"""Интерфейс стратегии и арена.

Контракт прост: стратегия смотрит на неизменяемый снимок и возвращает Signal или None.
Она не исполняет сделки, не видит другие стратегии и не может испортить им состояние.
"""
from __future__ import annotations

import logging
from typing import Optional

from .types import MarketSnapshot, Signal

log = logging.getLogger("polylab.arena")


class Strategy:
    """Базовый класс. Наследники переопределяют on_snapshot()."""

    name = "unnamed"
    version = "0.1.0"
    description = ""
    # Параметры стратегии. config_version меняется вручную при их изменении —
    # так в журнале видно, какая настройка породила сигнал.
    params: dict = {}
    config_version = "1"

    def __init__(self, **overrides):
        self.params = {**self.params, **overrides}
        self._state: dict = {}      # приватное состояние, чужим не видно

    def on_snapshot(self, snap: MarketSnapshot) -> Optional[Signal]:
        raise NotImplementedError

    # ── вспомогательное ──
    def signal(self, snap: MarketSnapshot, direction: str, entry: float,
               confidence: float, size_hint: float = 0.0, reason: str = "", **meta) -> Signal:
        return Signal(
            strategy=self.name, strategy_version=self.version, config_version=self.config_version,
            market_id=snap.market_id, asset=snap.asset, window_minutes=snap.window_minutes,
            direction=direction, entry_price=entry, confidence=confidence,
            size_hint=size_hint, reason=reason, meta=meta,
        )

    def state(self) -> dict:
        return dict(self._state)

    def load_state(self, d: dict) -> None:
        self._state = dict(d or {})


class Arena:
    """Прогоняет снимок через все стратегии, изолируя их друг от друга.

    Исключение внутри одной стратегии логируется и не мешает остальным.
    """

    def __init__(self, strategies: list[Strategy]):
        names = [s.name for s in strategies]
        dup = {n for n in names if names.count(n) > 1}
        if dup:
            raise ValueError(f"дублирующиеся имена стратегий: {dup}")
        self.strategies = strategies
        self.errors: dict[str, int] = {}

    def collect(self, snap: MarketSnapshot) -> list[Signal]:
        """Сигналы всех стратегий по одному снимку."""
        out = []
        for s in self.strategies:
            try:
                sig = s.on_snapshot(snap)
            except Exception as e:                      # изоляция сбоя
                self.errors[s.name] = self.errors.get(s.name, 0) + 1
                log.warning("стратегия %s упала на %s: %s", s.name, snap.market_id, e)
                continue
            if sig is None:
                continue
            if sig.strategy != s.name:                  # защита от подмены авторства
                log.warning("стратегия %s вернула сигнал с чужим именем %s", s.name, sig.strategy)
                continue
            out.append(sig)
        return out

    def state(self) -> dict:
        return {s.name: s.state() for s in self.strategies}

    def load_state(self, d: dict) -> None:
        for s in self.strategies:
            s.load_state((d or {}).get(s.name))
