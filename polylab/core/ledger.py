"""Универсальный журнал сигналов POLYLAB.

Один файл на все стратегии. Каждая запись знает, какая версия какой стратегии
её породила. Hypothetical и realized живут в разных колонках и не смешиваются
нигде — ни в записи, ни в агрегатах.
"""
from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timezone
from typing import Iterable, Optional

LEDGER_FILE = os.getenv("LEDGER_FILE", "data/signals.csv")

# Порядок фиксирован. Новые поля дописываются ТОЛЬКО в конец, иначе старые
# записи станут нечитаемыми.
FIELDS = [
    # что и когда
    "ts", "strategy", "strategy_version", "config_version", "feature_version",
    "mode", "asset", "market_id", "window", "direction",
    # состояние рынка в момент сигнала
    "elapsed", "remaining_sec", "reference_price", "current_price", "move_pct",
    "entry_price", "bid", "ask", "spread", "depth_usd", "imbalance",
    "confidence", "regime", "entry_bucket", "move_bucket", "elapsed_bucket",
    # решение портфеля и риска
    "decision", "decision_reason", "risk_gate", "size_requested", "size_granted",
    # исполнение
    "execution_state", "execution_quality", "filled_shares", "remaining_shares",
    "average_fill_price", "slippage",
    # результат — раздельно
    "resolution", "hypothetical_pnl", "realized_pnl",
    # контекст на момент сигнала
    "bankroll", "exposure", "open_positions", "meta",
]

DECISIONS = ("ACCEPT", "REDUCE", "REJECT", "CONFLICT")
EXEC_STATES = ("SHADOW", "SUBMITTED", "FILLED", "PARTIAL", "UNFILLED", "CANCELLED", "SKIPPED")


def _row_to_line(row: dict) -> str:
    out = []
    for k in FIELDS:
        v = row.get(k, "")
        if isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False, separators=(";", ":"))
        out.append(str(v).replace(",", ";").replace("\n", " "))
    return ",".join(out)


class Ledger:
    """Пишет и читает журнал. Незакрытые записи держит в памяти до резолва."""

    def __init__(self, path: str = LEDGER_FILE):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.pending: list[dict] = []

    # ── запись ──
    def append(self, row: dict) -> dict:
        new = not os.path.exists(self.path)
        with open(self.path, "a", encoding="utf-8") as f:
            if new:
                f.write(",".join(FIELDS) + "\n")
            f.write(_row_to_line(row) + "\n")
        return row

    def add_pending(self, row: dict, resolve_at: datetime, resolver_key: dict) -> dict:
        """Сигнал записан, но исход ещё не известен — ждёт конца окна."""
        self.pending.append({"row": row, "resolve_at": resolve_at.isoformat(), **resolver_key})
        return row

    def resolve_pending(self, now: datetime, price_at) -> list[dict]:
        """Дописывает исход тем записям, чьё окно закрылось.

        price_at(asset, dt) -> float | None. Записи, для которых цена недоступна,
        остаются в очереди и будут разрешены позже.
        """
        done = []
        for p in list(self.pending):
            if now < datetime.fromisoformat(p["resolve_at"]):
                continue
            final = price_at(p["asset"], datetime.fromisoformat(p["resolve_at"]))
            if final is None:
                continue
            went_up = final > p["reference_price"]
            won = (p["direction"] == "UP") == went_up
            row = p["row"]
            row["resolution"] = "WIN" if won else "LOSS"
            hyp_cost = float(row.get("size_requested") or 0)
            entry = float(row.get("entry_price") or 0)
            if hyp_cost and entry:
                shares = hyp_cost / entry
                row["hypothetical_pnl"] = round(shares - hyp_cost, 2) if won else round(-hyp_cost, 2)
            # realized заполняется исполнением, здесь не трогаем
            self.append(row)
            self.pending.remove(p)
            done.append(row)
        return done

    # ── чтение ──
    def read(self, limit: Optional[int] = None) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        num = {"elapsed", "remaining_sec", "reference_price", "current_price", "move_pct",
               "entry_price", "bid", "ask", "spread", "depth_usd", "imbalance", "confidence",
               "size_requested", "size_granted", "filled_shares", "remaining_shares",
               "average_fill_price", "slippage", "hypothetical_pnl", "realized_pnl",
               "bankroll", "exposure", "open_positions"}
        rows = []
        with open(self.path, encoding="utf-8", errors="ignore") as f:
            for r in csv.DictReader(f):
                row = {}
                for k, v in r.items():
                    if k in num:
                        try:
                            row[k] = float(v) if v not in ("", None) else None
                        except ValueError:
                            row[k] = None
                    else:
                        row[k] = v
                rows.append(row)
        return rows[-limit:] if limit else rows

    # ── состояние для рестарта ──
    def state(self) -> dict:
        return {"pending": self.pending}

    def load_state(self, d: dict) -> None:
        self.pending = list(d.get("pending") or [])


def summarize(rows: Iterable[dict], key=None) -> dict:
    """Агрегат по журналу. Realized и hypothetical считаются раздельно — всегда."""
    rows = [r for r in rows if r.get("resolution")]
    groups: dict = {}
    for r in rows:
        k = key(r) if key else "ALL"
        g = groups.setdefault(k, {"n": 0, "wins": 0, "losses": 0,
                                  "hyp_pnl": 0.0, "hyp_gp": 0.0, "hyp_gl": 0.0,
                                  "real_pnl": 0.0, "real_n": 0,
                                  "executed": 0, "shadow": 0, "rejected": 0})
        g["n"] += 1
        won = r.get("resolution") == "WIN"
        g["wins" if won else "losses"] += 1
        h = r.get("hypothetical_pnl") or 0.0
        g["hyp_pnl"] += h
        if h >= 0: g["hyp_gp"] += h
        else: g["hyp_gl"] += -h
        rl = r.get("realized_pnl")
        if rl is not None:
            g["real_pnl"] += rl; g["real_n"] += 1
        st = r.get("execution_state")
        if st in ("FILLED", "PARTIAL"): g["executed"] += 1
        elif st == "SHADOW": g["shadow"] += 1
        if r.get("decision") in ("REJECT", "CONFLICT"): g["rejected"] += 1
    for g in groups.values():
        g["winrate"] = g["wins"] / g["n"] if g["n"] else 0.0
        g["hyp_pf"] = round(g["hyp_gp"] / g["hyp_gl"], 2) if g["hyp_gl"] else (99.0 if g["hyp_gp"] else 0.0)
        g["hyp_pnl"] = round(g["hyp_pnl"], 2)
        g["real_pnl"] = round(g["real_pnl"], 2)
        for k in ("hyp_gp", "hyp_gl"):
            g.pop(k)
    return groups
