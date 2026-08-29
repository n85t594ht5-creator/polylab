"""Хранилище POLYLAB (архитектура 5C).

Решение принято по измерению, а не по плану. Эксперимент: 14 дней данных,
ежедневные коммиты, затем удаление raw старше 7 дней и `gc --prune=now`.
Рабочее дерево уменьшилось с 49 МБ до 27 МБ, а .git остался 45 МБ — объекты
достижимы из истории. Прирост .git ≈ 3.2 МБ/сутки → ≈ 1.2 ГБ/год только по DNA.
Вывод: raw в Git не коммитится вообще.

    data/raw/**      raw DNA и latency — .gitignore, уходят артефактом Actions
    data/agg/*.json  дневные агрегаты — Git, долгосрочно
    data/moves/*.gz  движения с исходами (размеченный датасет) — Git
    data/state/ids   индекс id для идемпотентности — Git, с обрезкой по времени

Индекс id обрезается по времени: snapshot_id = market_id@bucket, а окно
закрывается навсегда, поэтому id старше IDS_KEEP_HOURS повториться не может.
Так индекс не растёт бесконечно, но идемпотентность сохраняется.
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import os
import time
import zlib
from datetime import datetime, timedelta, timezone

SCHEMA_VERSION = "2"          # 5C: перенос raw из Git
IDS_KEEP_HOURS = int(os.getenv("IDS_KEEP_HOURS", "6"))
LOCK_STALE_SEC = int(os.getenv("LOCK_STALE_SEC", "1800"))

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
    "move_id", "detected_at", "market_id", "asset", "window", "move_start", "initial_move",
    "duration_so_far_sec", "acceleration_so_far", "elapsed_at_detect", "direction",
    "entry_ask_at_detect", "volatility_at_detect",
    "outcome_ready", "move_end", "move_duration_sec", "max_continuation", "max_reversal",
    "time_to_reversal_sec", "final_direction", "resolution",
    "schema_version", "collector_version",
]


def _day(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%d")


class CollectorLock:
    """Файловая блокировка против одновременных коллекторов.

    Concurrency-группа Actions защищает от параллельных прогонов в одном
    репозитории, но не от локального запуска рядом с ним. Замок закрывает
    остаток. Протухший замок (процесс убит) снимается по возрасту.
    """

    def __init__(self, path: str = "data/state/collector.lock"):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.acquired = False

    def acquire(self) -> bool:
        if os.path.exists(self.path):
            try:
                age = time.time() - os.path.getmtime(self.path)
            except OSError:
                age = 0
            if age < LOCK_STALE_SEC:
                return False
            os.remove(self.path)          # протух — снимаем
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()} {datetime.now(timezone.utc).isoformat()}".encode())
            os.close(fd)
            self.acquired = True
            return True
        except FileExistsError:
            return False

    def release(self) -> None:
        if self.acquired and os.path.exists(self.path):
            os.remove(self.path)
            self.acquired = False

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError("коллектор уже запущен (замок занят)")
        return self

    def __exit__(self, *a):
        self.release()


class Store:
    def __init__(self, root: str = "data"):
        self.root = root
        for d in ("raw/dna", "raw/latency", "moves", "agg", "state"):
            os.makedirs(os.path.join(root, d), exist_ok=True)
        self.seen: set[str] = set()
        self.written = 0
        self.duplicates = 0
        self._buf: list[dict] = []

    # ── пути ──
    def dna_path(self, ts: datetime) -> str:
        return os.path.join(self.root, "raw", "dna", f"{_day(ts)}.csv.gz")

    def latency_path(self, ts: datetime) -> str:
        return os.path.join(self.root, "raw", "latency", f"{_day(ts)}.csv")

    def moves_path(self, ts: datetime) -> str:
        return os.path.join(self.root, "moves", f"{_day(ts)}.csv")

    def agg_path(self, ts: datetime) -> str:
        return os.path.join(self.root, "agg", f"{_day(ts)}.json")

    def ids_path(self) -> str:
        return os.path.join(self.root, "state", "ids.txt")

    # ── индекс id: источник истины для идемпотентности ──
    def load_seen(self, ts: datetime | None = None) -> int:
        """Читает индекс id. Переживает обрыв: теряется максимум последняя строка."""
        p = self.ids_path()
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        sid = line.strip()
                        if sid:
                            self.seen.add(sid)
            except OSError:
                pass
        # дополняем тем, что удастся вычитать из raw текущих суток (best effort)
        if ts is not None:
            self.seen |= _read_gz_ids(self.dna_path(ts))
        return len(self.seen)

    def prune_ids(self, now: datetime, keep_hours: int = IDS_KEEP_HOURS) -> int:
        """Обрезает индекс по времени.

        Безопасно: snapshot_id = market_id@bucket, окно закрывается навсегда,
        значит id старше окна повториться не может.
        """
        cutoff = int(now.timestamp()) - keep_hours * 3600
        kept = set()
        for sid in self.seen:
            try:
                if int(sid.rsplit("@", 1)[1]) >= cutoff:
                    kept.add(sid)
            except (IndexError, ValueError):
                kept.add(sid)          # непонятный формат — не выбрасываем
        removed = len(self.seen) - len(kept)
        self.seen = kept
        self._write_ids_atomic()
        return removed

    def _write_ids_atomic(self) -> None:
        """Атомарная перезапись индекса: пишем во временный файл и заменяем."""
        p = self.ids_path()
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(sorted(self.seen)) + ("\n" if self.seen else ""))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)

    # ── DNA ──
    def add_dna(self, row: dict) -> bool:
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
        # индекс пишем первым: лучше знать о лишнем id, чем записать дубль
        with open(self.ids_path(), "a", encoding="utf-8") as f:
            f.write("".join(r["snapshot_id"] + "\n" for r in self._buf))
            f.flush()
            os.fsync(f.fileno())
        p = self.dna_path(ts)
        new = not os.path.exists(p)
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=DNA_FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        for r in self._buf:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in DNA_FIELDS})
        with gzip.open(p, "wt" if new else "at", encoding="utf-8") as f:
            f.write(buf.getvalue())
        n, self.written = len(self._buf), self.written + len(self._buf)
        self._buf.clear()
        return n

    # ── прочие журналы ──
    def append(self, kind: str, fields: list, row: dict, ts: datetime) -> None:
        path = {"latency": self.latency_path(ts), "moves": self.moves_path(ts)}.get(kind)
        if path is None:
            path = os.path.join(self.root, kind, f"{_day(ts)}.csv")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        new = not os.path.exists(path)
        with open(path, "a", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in fields})

    # ── ротация ──
    def rotate(self, now: datetime, keep: dict | None = None) -> dict:
        """Чистит только то, что реально хранится локально/в Git.

        raw живёт в артефактах Actions (публичный репозиторий — бесплатно,
        retention до 90 дней), поэтому локальная ротация raw короткая.
        """
        keep = keep or {"raw/dna": 2, "raw/latency": 2, "moves": 90, "agg": 3650}
        removed = {}
        for kind, days in keep.items():
            cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%d")
            d = os.path.join(self.root, kind)
            gone = []
            if os.path.isdir(d):
                for fn in sorted(os.listdir(d)):
                    day = fn.split(".")[0]
                    if len(day) == 10 and day < cutoff:
                        os.remove(os.path.join(d, fn))
                        gone.append(fn)
            removed[kind] = gone
        return removed

    def size_report(self) -> dict:
        out = {}
        for kind in ("raw/dna", "raw/latency", "moves", "agg", "state"):
            d = os.path.join(self.root, kind)
            files = [os.path.join(d, f) for f in os.listdir(d)] if os.path.isdir(d) else []
            out[kind] = {"files": len(files), "bytes": sum(os.path.getsize(f) for f in files)}
        out["total_bytes"] = sum(v["bytes"] for v in out.values() if isinstance(v, dict))
        out["git_tracked_bytes"] = sum(out[k]["bytes"] for k in ("moves", "agg", "state"))
        out["artifact_bytes"] = sum(out[k]["bytes"] for k in ("raw/dna", "raw/latency"))
        return out


def read_gz_rows(path: str) -> tuple:
    """Читает gz построчно, переживая обрыв. Возвращает (строки, был_ли_обрыв).

    Нужно потому, что gzip — единый deflate-поток: без этого повреждение хвоста
    файла лишает нас агрегата за целые сутки, а не только последних записей.
    """
    if not os.path.exists(path):
        return [], False
    lines: list[str] = []
    truncated = False
    try:
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
            while True:
                try:
                    line = f.readline()
                except (EOFError, OSError, gzip.BadGzipFile, zlib.error):
                    truncated = True
                    break
                if not line:
                    break
                lines.append(line)
    except (OSError, gzip.BadGzipFile, EOFError, zlib.error):
        return [], True
    if len(lines) < 2:
        return [], truncated or bool(lines)
    try:
        return list(csv.DictReader(lines)), truncated
    except csv.Error:
        return [], True


def _read_gz_ids(path: str) -> set:
    """Best-effort чтение id из gz. Обрыв gzip делает файл нечитаемым целиком —
    это измерено, поэтому основной источник истины всё равно индекс."""
    if not os.path.exists(path):
        return set()
    lines: list[str] = []
    try:
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
            while True:
                try:
                    line = f.readline()
                except (EOFError, OSError, gzip.BadGzipFile, zlib.error):
                    break
                if not line:
                    break
                lines.append(line)
    except (OSError, gzip.BadGzipFile, EOFError, zlib.error):
        return set()
    if len(lines) < 2:
        return set()
    out = set()
    try:
        for r in csv.DictReader(lines):
            sid = (r or {}).get("snapshot_id")
            if sid:
                out.add(sid)
    except csv.Error:
        pass
    return out
