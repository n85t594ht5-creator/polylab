"""Базовые типы POLYLAB. Неизменяемые снимки и сигналы — основа изоляции стратегий."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Optional

FEATURE_VERSION = "1"

# Режимы исполнения. LIVE отсутствует намеренно: включить его можно только
# осознанной правкой кода, автоматического перехода не существует.
EXECUTION_MODES = ("backtest", "shadow", "paper")


@dataclass(frozen=True)
class OrderBook:
    """Снимок стакана одного исхода."""
    bid: Optional[float] = None
    ask: Optional[float] = None
    spread: Optional[float] = None
    depth_usd: Optional[float] = None
    levels: tuple = ()                 # ((цена, размер), ...) по возрастанию цены
    imbalance: Optional[float] = None  # (объём bid − объём ask) / сумма

    def to_dict(self) -> dict:
        return {k: (list(v) if isinstance(v, tuple) else v) for k, v in asdict(self).items()}


@dataclass(frozen=True)
class MarketSnapshot:
    """Полный снимок одного рынка в один момент — вход для всех стратегий.

    Неизменяемый: стратегия физически не может испортить данные соседям.
    """
    ts: datetime
    asset: str
    market_id: str
    window_minutes: int
    start: datetime
    end: datetime
    elapsed: float                  # доля прошедшего окна, 0..1
    remaining_sec: float
    reference_price: float          # цена underlying на старте окна
    current_price: float
    move: float                     # (current − reference) / reference
    up_token: str = ""
    down_token: str = ""
    up_ask: Optional[float] = None
    down_ask: Optional[float] = None
    up_book: Optional[OrderBook] = None
    down_book: Optional[OrderBook] = None
    features: dict = field(default_factory=dict)   # Market DNA, см. features.py
    feature_version: str = FEATURE_VERSION

    def ask_for(self, direction: str) -> Optional[float]:
        return self.up_ask if direction == "UP" else self.down_ask

    def book_for(self, direction: str) -> Optional[OrderBook]:
        return self.up_book if direction == "UP" else self.down_book

    def token_for(self, direction: str) -> str:
        return self.up_token if direction == "UP" else self.down_token


@dataclass
class Signal:
    """Что стратегия хочет сделать. Исполнение решает не она."""
    strategy: str
    strategy_version: str
    config_version: str
    market_id: str
    asset: str
    window_minutes: int
    direction: str                  # UP | DOWN
    entry_price: float              # цена, по которой стратегия рассчитывает войти
    confidence: float               # 0..1, эвристика стратегии — НЕ вероятность
    size_hint: float = 0.0          # желаемый размер в $, портфель может уменьшить
    reason: str = ""                # почему вошли — человекочитаемо
    meta: dict = field(default_factory=dict)   # признаки, пороги, всё специфичное

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Outcome:
    """Исход, известный ТОЛЬКО после закрытия окна.

    Связан со снимком по snapshot_id и живёт отдельно от него. Признаки снимка
    заморожены (MarketSnapshot — frozen), поэтому исход физически не может
    попасть обратно в вектор признаков. Это ключевой инвариант POLYLAB.
    """
    snapshot_id: str
    resolved_at: datetime
    final_price: float
    final_direction: str            # UP | DOWN
    resolution: str                 # CONTINUED | REVERSED | WIN | LOSS
    max_continuation: Optional[float] = None
    max_reversal: Optional[float] = None
    time_to_reversal_sec: Optional[float] = None

    # Поля, которые НИКОГДА не должны появиться в признаках снимка.
    FORBIDDEN_IN_FEATURES = ("final_price", "final_direction", "resolution",
                             "max_continuation", "max_reversal", "time_to_reversal_sec")

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items()}


@dataclass
class Decision:
    """Вердикт Portfolio Manager по сигналу."""
    action: str                     # ACCEPT | REJECT | REDUCE | CONFLICT
    size: float = 0.0
    reason: str = ""
    gate: str = ""                  # какой лимит сработал

    @property
    def accepted(self) -> bool:
        return self.action in ("ACCEPT", "REDUCE")
