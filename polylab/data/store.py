"""Хранилище POLYLAB.

Правила из DATA_SCHEMA.md:
  raw DNA         → data/dna/YYYY-MM-DD.csv.gz, ротация 14 дней
  latency         → data/latency/YYYY-MM-DD.csv, 90 дней
  moves           → data/moves/YYYY-MM-DD.csv, 90 дней
  raw order book  → в репозиторий НЕ пишем (артефакт Actions)

Идемпотентность: id уже записанных снимков текущих суток держим в множестве и
восстанавливаем из файла при старте. Повторный прогон в том же временном ведре
дубля не создаёт.
"""
from __future__ import annotations

import csv
import gzip
import io
import os
from datetime import datetime, timedelta, timezone

SCHEMA_VERSION = "1"

DNA_FIELDS = [
    "snapshot_id", "ts", "schema_version", "feature_version", "collector_version",
    "market_id", "slug", "asset", "window", "start", "end", "elapsed", "time_remaining",
    "reference_price", "current_price", "move", "direction",
    "up_ask", "down_ask",
    "best_bid_up", "best_ask_up", "spread_up", "bid_depth_up", "ask_depth_up", "imbalance_up",
    "depth_1_up", "depth_2_up", "depth_5_up", "depth_10_up", "book_ts_up", "n_ask_levels_up",
    "best_bid_dn", "best_ask_dn", "spread_dn", "bid_depth_dn", "ask_depth_dn", "imbalance_dn",
    "depth_1_dn", "book_ts_dn",
    "volatility", "acceleration", "reversals_so_far", "samples_so_far",
    "liquidity", "tick_size", "market_volume",
    "cb_trade_time", "cb_trade_id", "cb_volume",
    "quality", "missing", "stale_ms", "source",
]

LATENCY_FIELDS = ["event_id", "asset", "market_id", "window", "source_time", "observation_time",
                  "market_time", "reaction_time", "observed_diff_ms", "move_pct",
                  "poll_interval_ms", "quality", "note", "collector_version"]

MOVE_FIELDS = [
    # признаки — известны в момент обнаружения
    "move_id", "detected_at", "market_id", "asset", "window", "move_start", "initial_move",
    "duration_so_far_sec", "acceleration_so_far", "elapsed_at_detect", "direction",
    "entry_ask_at_detect", "volatility_at_detect",
    # исход — становится известен ПОЗЖЕ, заполняется отдельно
    "outcome_ready", "move_end", "move_duration_sec", "max_continuation", "max_reversal",
    "time_to_reversal_sec", "final_direction", "resolution",
    "schema_version", "collector_version",
]


def _day(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%d")


class Store:
    def __init__(self, root: str = "data"):
        self.root = root
        for d in ("dna", "latency", "moves", "agg"):
            os.makedirs(os.path.join(root, d), exist_ok=True)
        self.seen: set[str] = set()
        self.written = 0
        self.duplicates = 0
        self._buf: list[dict] = []

    # ── DNA ──
    def dna_path(self, ts: datetime) -> str:
        return os.path.join(self.root, "dna", f"{_day(ts)}.csv.gz")

    def load_seen(self, ts: datetime) -> int:
        """Восстанавливает id уже записанных снимков суток — защита от дублей после рестарта."""
        p = self.dna_path(ts)
        if not os.path.exists(p):
            return 0
        try:
            with gzip.open(p, "rt", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    self.seen.add(r["snapshot_id"])
        except Exception:
            pass
        return len(self.seen)

    def add_dna(self, row: dict) -> bool:
        """False — если такой снимок уже записан (идемпотентность)."""
        sid = row["snapshot_id"]
        if sid in self.seen:
            self.duplicates += 1
            return False
        self.seen.add(sid)
        self._buf.append(row)
        return True

    def flush_dna(self, ts: datetime) -> int:
        if not self._buf:
            return 0
        p = self.dna_path(ts)
        new = not os.path.exists(p)
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=DNA_FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        for r in self._buf:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in DNA_FIELDS})
        mode = "wt" if new else "at"
        with gzip.open(p, mode, encoding="utf-8") as f:
            f.write(buf.getvalue())
        n, self.written = len(self._buf), self.written + len(self._buf)
        self._buf.clear()
        return n

    # ── прочие журналы ──
    def append(self, kind: str, fields: list, row: dict, ts: datetime) -> None:
        p = os.path.join(self.root, kind, f"{_day(ts)}.csv")
        new = not os.path.exists(p)
        with open(p, "a", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in fields})

    # ── ротация ──
    def rotate(self, now: datetime, keep: dict | None = None) -> dict:
        keep = keep or {"dna": 14, "latency": 90, "moves": 90}
        removed = {}
        for kind, days in keep.items():
            cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%d")
            d = os.path.join(self.root, kind)
            gone = []
            for fn in sorted(os.listdir(d)) if os.path.isdir(d) else []:
                day = fn.split(".")[0]
                if len(day) == 10 and day < cutoff:
                    os.remove(os.path.join(d, fn))
                    gone.append(fn)
            removed[kind] = gone
        return removed

    def size_report(self) -> dict:
        out = {}
        for kind in ("dna", "latency", "moves", "agg"):
            d = os.path.join(self.root, kind)
            files = [os.path.join(d, f) for f in os.listdir(d)] if os.path.isdir(d) else []
            out[kind] = {"files": len(files),
                         "bytes": sum(os.path.getsize(f) for f in files)}
        out["total_bytes"] = sum(v["bytes"] for v in out.values() if isinstance(v, dict))
        return out
