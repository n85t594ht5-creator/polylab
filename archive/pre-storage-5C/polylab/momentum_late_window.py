"""Momentum / late-window favorite.

Та самая гипотеза, которую проверяет Polybot (PROJECT A). Здесь она —
обычный участник арены, один из многих, а не привилегированная логика.

Параметры скопированы с locked baseline PROJECT A, чтобы результаты
можно было сравнивать напрямую. Менять их здесь можно свободно:
на PROJECT A это никак не влияет.
"""
from __future__ import annotations

from typing import Optional

from ..core.strategy import Strategy
from ..core.types import MarketSnapshot, Signal


class MomentumLateWindow(Strategy):
    name = "momentum_late_window"
    version = "1.0.0"
    config_version = "baseline-A-1.5.0"
    description = ("Вход в последней четверти окна на стороне уже случившегося движения, "
                   "пока нужный исход ещё стоит 0.50–0.62.")
    params = {
        "min_elapsed": 0.75,
        "min_entry": 0.50,
        "max_entry": 0.62,
        "tier_entry": 0.55,
        "min_move": 0.0010,
        "min_move_high": 0.0012,
        "min_conf": 0.70,
        "min_remaining_sec": 30,
        "stake_pct": 0.05,        # доля банкролла как ориентир размера
    }

    def on_snapshot(self, snap: MarketSnapshot) -> Optional[Signal]:
        p = self.params
        if snap.elapsed < p["min_elapsed"] or snap.remaining_sec < p["min_remaining_sec"]:
            return None
        if snap.move == 0:
            return None
        direction = "UP" if snap.move > 0 else "DOWN"
        ask = snap.ask_for(direction)
        if ask is None or not (p["min_entry"] <= ask <= p["max_entry"]):
            return None
        required = p["min_move_high"] if ask > p["tier_entry"] else p["min_move"]
        if abs(snap.move) < required:
            return None
        strength = min(abs(snap.move) / 0.005, 1)
        conf = min(0.95, 0.5 + snap.elapsed * 0.3 + strength * 0.15)
        if conf < p["min_conf"]:
            return None
        return self.signal(
            snap, direction, ask, round(conf, 3),
            size_hint=snap.features.get("bankroll", 1000.0) * p["stake_pct"],
            reason=f"движение {snap.move*100:+.3f}% ≥ {required*100:.2f}%, вход {ask:.3f}",
            required_move=required, elapsed=round(snap.elapsed, 3),
        )
