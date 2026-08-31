"""Датасет для бэктеста: только реально собранные данные POLYLAB.

Никаких синтетических цен в продовом пути. Источник каждой записи помечен,
и данные разных источников не смешиваются.
"""
from __future__ import annotations

import csv, gzip, json, os, sys
from datetime import datetime, timezone
from typing import Iterator

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from polylab.data.store import read_gz_rows  # noqa: E402

DATASET_VERSION = "1"


def _f(v):
    if v in ("", None):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class Dataset:
    """Снимки Market DNA, сгруппированные по рынку и упорядоченные по времени."""

    def __init__(self, root: str = "data", source: str = "REAL"):
        self.root, self.source = root, source
        self.rows: list[dict] = []
        self.days: list[str] = []
        self.issues: list[str] = []

    def load(self) -> "Dataset":
        d = os.path.join(self.root, "raw", "dna")
        files = sorted(f for f in os.listdir(d) if f.endswith(".csv.gz")) if os.path.isdir(d) else []
        for fn in files:
            rows, truncated = read_gz_rows(os.path.join(d, fn))
            if truncated:
                self.issues.append(f"{fn}: файл оборван, взяты доступные строки")
            self.rows.extend(rows)
            if rows:
                self.days.append(fn[:-7])
        self.rows.sort(key=lambda r: r.get("ts") or "")
        return self

    # ── свойства выборки ──
    def stats(self) -> dict:
        usable = [r for r in self.rows if r.get("quality") not in ("INVALID",)
                  and _f(r.get("reference_price")) and _f(r.get("current_price"))]
        markets = {r.get("market_id") for r in usable}
        by_win: dict = {}
        for r in usable:
            by_win[r.get("window") or "?"] = by_win.get(r.get("window") or "?", 0) + 1
        span_h = None
        if usable:
            try:
                t0 = datetime.fromisoformat(usable[0]["ts"])
                t1 = datetime.fromisoformat(usable[-1]["ts"])
                span_h = round((t1 - t0).total_seconds() / 3600, 2)
            except Exception:
                pass
        return {"source": self.source, "dataset_version": DATASET_VERSION,
                "rows_total": len(self.rows), "rows_usable": len(usable),
                "days": len(self.days), "day_list": self.days,
                "unique_markets": len(markets), "by_window": by_win,
                "span_hours": span_h, "issues": self.issues}

    def by_market(self) -> dict:
        """market_id -> упорядоченный список снимков этого окна."""
        out: dict = {}
        for r in self.rows:
            if r.get("quality") == "INVALID":
                continue
            if not (_f(r.get("reference_price")) and _f(r.get("current_price"))):
                continue
            out.setdefault(r.get("market_id"), []).append(r)
        for v in out.values():
            v.sort(key=lambda r: r.get("ts") or "")
        return out

    def split(self, oos_fraction: float = 0.30) -> tuple:
        """Разделение по ВРЕМЕНИ: подбор только на in-sample.

        Граница проходит между окнами, а не внутри — иначе часть одного окна
        попала бы в обучение, часть в проверку.
        """
        mk = self.by_market()
        starts = sorted((v[0].get("start") or v[0].get("ts"), k) for k, v in mk.items())
        if not starts:
            return {}, {}, None
        cut = int(len(starts) * (1 - oos_fraction))
        cut = min(max(cut, 1), len(starts) - 1) if len(starts) > 1 else len(starts)
        is_ids = {k for _, k in starts[:cut]}
        oos_ids = {k for _, k in starts[cut:]}
        border = starts[cut][0] if cut < len(starts) else None
        return ({k: v for k, v in mk.items() if k in is_ids},
                {k: v for k, v in mk.items() if k in oos_ids}, border)
