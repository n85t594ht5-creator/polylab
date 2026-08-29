#!/usr/bin/env python3
"""Коллектор POLYLAB: Market DNA + Order Book + Latency + Fake Move.

Собирает данные. Не торгует, не принимает решений, не знает о стратегиях.
Все параметры сбора — только про сбор; торговая логика здесь отсутствует.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from polylab.core.features import build_features, quality, snapshot_id, FEATURE_VERSION
from polylab.data import sources as SRC
from polylab.data.store import LATENCY_FIELDS, MOVE_FIELDS, Store, SCHEMA_VERSION

SAMPLE_SEC = int(os.getenv("SAMPLE_SEC", "15"))       # частота снимков (5A: 15 с)
MIN_ELAPSED_COLLECT = float(os.getenv("MIN_ELAPSED_COLLECT", "0.4"))
ASSETS = [a.strip().upper() for a in os.getenv("ASSETS", "BTC,ETH,SOL,XRP").split(",")]
WINDOWS = [int(w) for w in os.getenv("WINDOWS", "5,15,60").split(",")]
RUN_SEC = int(os.getenv("RUN_SEC", "300"))            # длительность прогона
MOVE_TRIGGER = float(os.getenv("MOVE_TRIGGER", "0.0005"))   # порог фиксации движения

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-5s %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("collector")


class Collector:
    def __init__(self, store: Store):
        self.store = store
        self.hist: dict[str, list[dict]] = {}     # market_id -> снимки этого окна
        self.refs: dict[str, float] = {}          # market_id -> опорная цена
        self.moves: dict[str, dict] = {}          # активные движения (features уже зафиксированы)
        self.pending_moves: list[dict] = []       # ждут outcome после конца окна
        self.stats = {"snapshots": 0, "duplicates": 0, "markets": set(), "assets": set(),
                      "by_window": {}, "quality": {}, "latency_events": 0, "moves": 0,
                      "stale": 0, "incomplete": 0}

    # ── один проход по всем активным окнам ──
    def tick(self) -> None:
        now = SRC.now()
        markets = SRC.list_updown_markets(ASSETS, WINDOWS)
        if not markets:
            log.warning("рынки не найдены")
            return
        tickers = {}
        for a in {m["asset"] for m in markets}:
            t = SRC.ticker(a)
            if t:
                tickers[a] = {**t, "observed_at": SRC.now()}

        for m in markets:
            total = m["window_minutes"] * 60
            elapsed = (now - m["start"]).total_seconds() / total
            if elapsed < MIN_ELAPSED_COLLECT or elapsed > 1.0:
                continue
            sid = snapshot_id(m["market_id"], now.timestamp(), SAMPLE_SEC)
            if sid in self.store.seen:
                continue
            tk = tickers.get(m["asset"])
            if not tk:
                continue
            ref = self.refs.get(m["market_id"])
            if ref is None:
                ref = SRC.minute_ref(m["asset"], m["start"])
                if ref:
                    self.refs[m["market_id"]] = ref
            cur = tk["price"]
            book_up = SRC.book_metrics(SRC.fetch_book(m["up_token"]))
            book_dn = SRC.book_metrics(SRC.fetch_book(m["down_token"]))
            move = (cur - ref) / ref if (ref and cur) else None

            hist = self.hist.setdefault(m["market_id"], [])
            hist.append({"ts": now, "current_price": cur})
            feats = build_features(hist, m, book_up, book_dn)

            stale = None
            if book_up.get("book_ts"):
                stale = max(0.0, now.timestamp() * 1000 - book_up["book_ts"])

            row = {
                "snapshot_id": sid, "ts": now.isoformat(), "schema_version": SCHEMA_VERSION,
                "feature_version": FEATURE_VERSION, "collector_version": SRC.COLLECTOR_VERSION,
                "market_id": m["market_id"], "slug": m["slug"], "asset": m["asset"],
                "window": f"{m['window_minutes']}m", "start": m["start"].isoformat(),
                "end": m["end"].isoformat(), "elapsed": round(elapsed, 4),
                "time_remaining": round((m["end"] - now).total_seconds(), 1),
                "reference_price": ref, "current_price": cur,
                "move": round(move, 6) if move is not None else None,
                "direction": None if move is None else ("UP" if move > 0 else "DOWN" if move < 0 else "FLAT"),
                "up_ask": book_up.get("best_ask"), "down_ask": book_dn.get("best_ask"),
                "cb_trade_time": tk.get("time"), "cb_trade_id": tk.get("trade_id"),
                "cb_volume": tk.get("volume"), "stale_ms": None if stale is None else round(stale),
                "source": "coinbase+clob", **feats,
            }
            for pref, b in (("_up", book_up), ("_dn", book_dn)):
                row[f"best_bid{pref}"] = b.get("best_bid"); row[f"best_ask{pref}"] = b.get("best_ask")
                row[f"spread{pref}"] = b.get("spread"); row[f"bid_depth{pref}"] = b.get("bid_depth")
                row[f"ask_depth{pref}"] = b.get("ask_depth"); row[f"imbalance{pref}"] = b.get("imbalance")
                row[f"book_ts{pref}"] = b.get("book_ts")
                for n in (1, 2, 5, 10):
                    row[f"depth_{n}{pref}"] = b.get(f"depth_{n}")
                row[f"n_ask_levels{pref}"] = b.get("n_ask_levels")
            q, missing = quality(row, stale)
            row["quality"], row["missing"] = q, "|".join(missing)

            if self.store.add_dna(row):
                self.stats["snapshots"] += 1
                self.stats["markets"].add(m["market_id"]); self.stats["assets"].add(m["asset"])
                wk = f"{m['window_minutes']}m"
                self.stats["by_window"][wk] = self.stats["by_window"].get(wk, 0) + 1
                self.stats["quality"][q] = self.stats["quality"].get(q, 0) + 1
                if q == "STALE": self.stats["stale"] += 1
                if q == "INCOMPLETE": self.stats["incomplete"] += 1
            else:
                self.stats["duplicates"] += 1

            self._latency(m, tk, book_up, now)
            self._detect_move(m, row, now)

        self._close_moves(now)

    # ── latency: только наблюдаемая разница времён, без утверждений о причинности ──
    def _latency(self, m: dict, tk: dict, book: dict, now: datetime) -> None:
        src_t, mkt_t = tk.get("time"), book.get("book_ts")
        if not src_t or not mkt_t:
            return
        try:
            st = datetime.fromisoformat(src_t.replace("Z", "+00:00")[:26] + "+00:00").timestamp() * 1000 \
                if "+" not in src_t[10:] else None
        except Exception:
            st = None
        if st is None:
            try:
                st = datetime.fromisoformat(src_t.replace("Z", "+00:00")).timestamp() * 1000
            except Exception:
                return
        diff = mkt_t - st
        poll_ms = SAMPLE_SEC * 1000
        # честный флаг: меньше интервала опроса — измерить нельзя
        if abs(diff) >= 3 * poll_ms:
            qual = "HIGH"
        elif abs(diff) >= poll_ms:
            qual = "MEDIUM"
        else:
            qual = "LOW"
        self.store.append("latency", LATENCY_FIELDS, {
            "event_id": f"{m['market_id']}@{int(now.timestamp())}", "asset": m["asset"],
            "market_id": m["market_id"], "window": f"{m['window_minutes']}m",
            "source_time": src_t, "observation_time": now.isoformat(),
            "market_time": int(mkt_t), "reaction_time": None,
            "observed_diff_ms": round(diff),   # НАБЛЮДАЕМАЯ разница, не causal latency
            "move_pct": None, "poll_interval_ms": poll_ms, "quality": qual,
            "note": "observed timing difference; causality not established",
            "collector_version": SRC.COLLECTOR_VERSION}, now)
        self.stats["latency_events"] += 1

    # ── движения: признаки фиксируются в момент обнаружения, исход — только позже ──
    def _detect_move(self, m: dict, row: dict, now: datetime) -> None:
        mid = m["market_id"]
        move = row.get("move")
        if move is None or mid in self.moves:
            return
        if abs(move) < MOVE_TRIGGER:
            return
        self.moves[mid] = {
            "move_id": f"{mid}@{int(now.timestamp())}", "detected_at": now.isoformat(),
            "market_id": mid, "asset": m["asset"], "window": f"{m['window_minutes']}m",
            "move_start": row["start"], "initial_move": move,
            "duration_so_far_sec": round((now - m["start"]).total_seconds(), 1),
            "acceleration_so_far": row.get("acceleration"),
            "elapsed_at_detect": row["elapsed"], "direction": row["direction"],
            "entry_ask_at_detect": row["up_ask"] if move > 0 else row["down_ask"],
            "volatility_at_detect": row.get("volatility"),
            "schema_version": SCHEMA_VERSION, "collector_version": SRC.COLLECTOR_VERSION,
            # поля исхода намеренно пусты — они из будущего
            "outcome_ready": 0, "_ref": row["reference_price"], "_end": m["end"].isoformat(),
            "_peak": move, "_trough": move, "_t_rev": None,
        }
        self.stats["moves"] += 1

    def _close_moves(self, now: datetime) -> None:
        """Обновляет экстремумы и записывает движение с исходом после конца окна."""
        for mid, mv in list(self.moves.items()):
            hist = self.hist.get(mid) or []
            ref = mv["_ref"]
            if ref and hist:
                cur_move = (hist[-1]["current_price"] - ref) / ref
                sign = 1 if mv["initial_move"] > 0 else -1
                if cur_move * sign > mv["_peak"] * sign:
                    mv["_peak"] = cur_move
                if cur_move * sign < mv["_trough"] * sign:
                    mv["_trough"] = cur_move
                    if mv["_t_rev"] is None and cur_move * sign < 0:
                        mv["_t_rev"] = (hist[-1]["ts"] - datetime.fromisoformat(mv["detected_at"])).total_seconds()
            if now < datetime.fromisoformat(mv["_end"]) + timedelta(seconds=90):
                continue
            final = SRC.minute_ref(mv["asset"], datetime.fromisoformat(mv["_end"]))
            if final is None:
                continue
            went_up = final > ref
            row = {k: v for k, v in mv.items() if not k.startswith("_")}
            row.update({
                "outcome_ready": 1, "move_end": mv["_end"],
                "move_duration_sec": round((datetime.fromisoformat(mv["_end"])
                                            - datetime.fromisoformat(mv["detected_at"])).total_seconds(), 1),
                "max_continuation": round(mv["_peak"], 6), "max_reversal": round(mv["_trough"], 6),
                "time_to_reversal_sec": mv["_t_rev"],
                "final_direction": "UP" if went_up else "DOWN",
                "resolution": "CONTINUED" if (went_up == (mv["initial_move"] > 0)) else "REVERSED",
            })
            self.store.append("moves", MOVE_FIELDS, row, now)
            del self.moves[mid]

    def report(self) -> dict:
        return {**{k: (len(v) if isinstance(v, set) else v) for k, v in self.stats.items()},
                "api": SRC.STATS.as_dict()}


def main() -> None:
    store = Store()
    now = SRC.now()
    restored = store.load_seen(now)
    log.info("старт: sample=%ds, окна=%s, активы=%s, восстановлено id=%d",
             SAMPLE_SEC, WINDOWS, ASSETS, restored)
    c = Collector(store)
    t_end = time.time() + RUN_SEC
    while time.time() < t_end:
        t0 = time.time()
        try:
            c.tick()
        except Exception as e:
            log.error("ошибка прохода: %s", e)
        store.flush_dna(SRC.now())
        time.sleep(max(0.0, SAMPLE_SEC - (time.time() - t0)))
    store.flush_dna(SRC.now())
    rep = {**c.report(), "storage": store.size_report(),
           "duplicates_store": store.duplicates, "restored_ids": restored,
           "run_sec": RUN_SEC, "sample_sec": SAMPLE_SEC,
           "finished_at": SRC.now().isoformat()}
    os.makedirs("research", exist_ok=True)
    json.dump(rep, open("research/COLLECTOR_RUN.json", "w"), ensure_ascii=False, indent=1, default=str)
    log.info("итог: %s", json.dumps(rep, ensure_ascii=False, default=str)[:800])


if __name__ == "__main__":
    main()
