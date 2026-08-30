# POLYLAB — архитектура

## Решение по итогам аудита PROJECT A

Аудит `bot.py` (775 строк) показал, что код делится на две несмешивающиеся части:

| Слой | Судьба |
|---|---|
| Рыночные данные (`find_updown_markets`, `clob_ask`, `clob_book`, `fillable`, цены) | переносится как базовый слой |
| Классификаторы (бакеты входа/движения/elapsed, `exec_quality`) | переносится в feature engine |
| Журнал сигналов | переносится, расширяется полями стратегии и версий |
| Исполнение и резолв | переносится в execution engine |
| **`evaluate()`** | **не переносится** — это одна конкретная гипотеза, в POLYLAB она станет обычной стратегией `momentum_late_window` наравне с остальными |

Ключевое отличие POLYLAB от Polybot: в Polybot стратегия зашита в цикл.
Здесь цикл ничего не знает о стратегиях, а стратегии ничего не знают друг о друге.

## Поток

```
                    MARKET DATA
              (рынки, цены, стакан)
                         │
                   FEATURE ENGINE
        (снимок рынка → набор признаков = Market DNA)
                         │
                  STRATEGY ARENA
        ┌────────────┬───┴────┬─────────────┐
     momentum    fake_move  latency     mean_rev …
        └────────────┴───┬────┴─────────────┘
                         │  (независимые Signal)
                  PORTFOLIO MANAGER
        (accept / reject / reduce / conflict)
                         │
                    RISK ENGINE
                         │
                 EXECUTION ENGINE
              (shadow: ничего не шлёт)
                         │
                  SIGNAL LEDGER
        (единый журнал: hypothetical ↔ realized)
```

## Изоляция стратегий

Требование «одна стратегия не меняет состояние другой» обеспечивается тремя правилами:

1. Стратегия получает **неизменяемый** `MarketSnapshot` (frozen dataclass) и свой
   приватный `dict` состояния. Ни арены, ни чужого состояния она не видит.
2. Стратегия **не исполняет** сделки — она только возвращает `Signal` или `None`.
   Решение принимает Portfolio Manager, исполняет Execution Engine.
3. Исключение в одной стратегии ловится ареной, логируется и не прерывает остальные.

## Версионирование

В каждой записи журнала сохраняются `strategy_version`, `config_version`,
`feature_version`. Через месяцы будет видно, какая именно версия породила сигнал.

## Режимы

`BACKTEST → SHADOW → FORWARD → PAPER → TINY LIVE → LIVE`

LIVE отключён на уровне кода: `EXECUTION_MODES` не содержит live, попытка
установить его вызывает ошибку. Автоперехода нет.

## Расширяемость на другие площадки (направление, не задача сейчас)

Ядро не должно зависеть от Polymarket. Уже сейчас:

- `MarketSnapshot` / `Signal` / `Decision` не содержат специфичных для Polymarket полей
  в обязательной части; специфика уходит в `features` и `meta`.
- Слой источников (`data/sources.py`) отделён от ядра — это фактически адаптер.
- В записях зонда есть `venue` и `instrument`, чтобы данные разных площадок
  не смешивались в одном файле без различения.

Целевая схема на будущее (Bybit Futures и другие **не реализуются сейчас**):

```
MarketDataAdapter (Polymarket | Bybit | …)
        ↓ нормализованный снимок
FEATURE ENGINE → STRATEGY → SIGNAL LEDGER → RISK → PORTFOLIO → ExecutionAdapter
```

Правило на время разработки: не зашивать в универсальные структуры допущения,
которые сделают невозможным описание фьючерса (плечо, фандинг, mark/index price).

## Фазы

- [x] PHASE 0 — backup A, независимый репозиторий B
- [x] PHASE 1 — аудит, архитектура
- [x] PHASE 2 — data layer + universal signal ledger
- [x] PHASE 3 — strategy interface + Shadow/Paper Arena
- [ ] PHASE 4 — Portfolio + Risk
- [ ] PHASE 5 — Market DNA / Latency / Fake Move / Order Book
- [ ] PHASE 6 — Dashboard
- [ ] PHASE 7 — Backtest / OOS / Forward
- [ ] PHASE 8 — Tests
- [ ] PHASE 9 — Deployment на отдельный Pages URL
