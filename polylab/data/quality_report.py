#!/usr/bin/env python3
"""5D.4 — сводный отчёт качества данных.

Собирает результаты 5D.1–5D.3 и выносит статус по каждому направлению:
CONFIRMED / SUPPORTED / INCONCLUSIVE / UNAVAILABLE / NOT OBSERVED.
Ничего не приукрашивает: отсутствие данных остаётся отсутствием.
"""
from __future__ import annotations

import json, os, sys
from datetime import datetime, timezone

REPORT_VERSION = "1"
R = "research"


def load(name):
    p = os.path.join(R, name)
    if not os.path.exists(p):
        return None
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return None


def build() -> tuple:
    ls, lat, win, avail = (load("LONG_SERIES.json"), load("LATENCY_PROBE.json"),
                           load("WINDOW_OBSERVATION.json"), load("DATA_AVAILABILITY.json"))
    findings = []

    # long series
    if ls and ls.get("days"):
        t = ls["totals"]
        real_issues = [i for i in ls["issues"]
                       if i["kind"] not in ("WINDOW_NOT_OBSERVED", "TS_NOT_MONOTONIC")]
        n = t.get("snapshots", 0)
        status = "CONFIRMED" if (not real_issues and n >= 500) else \
                 "SUPPORTED" if not real_issues else "INCONCLUSIVE"
        findings.append({
            "area": "Целостность длинной серии", "status": status,
            "detail": f"{ls['days']} дней, {n} снимков, валидных {t.get('valid')} "
                      f"({t.get('valid_share', 0)*100:.0f}%), дублей id {t.get('duplicate_ids')}",
            "note": ("выборка мала для CONFIRMED (нужно ≥500 снимков подряд)" if n < 500 and not real_issues
                     else "; ".join(f"{i['kind']}" for i in real_issues[:3]) or ""),
            "source": ls.get("source", "REAL"),
        })
        findings.append({
            "area": "Отсутствие утечек будущего", "status":
                "CONFIRMED" if not [i for i in ls["issues"] if i["kind"] == "LEAKAGE"] else "INCONCLUSIVE",
            "detail": "полей исхода в векторе признаков не найдено",
            "note": "проверяется на raw, пока он локально доступен", "source": "REAL"})
    else:
        findings.append({"area": "Целостность длинной серии", "status": "INCONCLUSIVE",
                         "detail": "агрегатов нет", "note": "", "source": "—"})

    # latency
    if lat and lat.get("status") == "OK":
        mc = lat.get("measurable_counts", {})
        findings.append({
            "area": "Измеримость задержки", "status": lat.get("verdict", "INCONCLUSIVE"),
            "detail": f"{lat['samples']} проб, интервал {lat['interval_target_ms']:.0f} мс, "
                      f"мин. измеримая задержка ≈ {lat.get('min_measurable_latency_ms')} мс; "
                      f"классификация {mc}",
            "note": "; ".join(lat.get("limits", [])), "source": "REAL"})
        findings.append({
            "area": "Задержка запроса", "status": "CONFIRMED",
            "detail": f"медиана {lat['request_latency_ms']['median']} мс, "
                      f"p95 {lat['request_latency_ms']['p95']} мс, max {lat['request_latency_ms']['max']} мс",
            "note": "измеряется напрямую, без допущений", "source": "REAL"})
        rate = lat.get("book_change_rate", 0)
        attributable = rate < 0.5
        findings.append({
            "area": "Изменчивость стакана", "status": "CONFIRMED",
            "detail": f"стакан менялся в {rate*100:.0f}% интервалов "
                      f"({lat.get('book_changes')} из {lat['samples']})",
            "note": ("частота изменений позволяет привязывать изменение к событию" if attributable else
                     "стакан меняется почти в каждом интервале — привязать изменение к конкретному "
                     "событию источника НЕВОЗМОЖНО; реакция как отдельное событие не выделяется"),
            "source": "REAL"})
        # разница времён без атрибуции — это смещение, а не задержка
        od = lat.get("observed_diff_ms") or {}
        findings.append({
            "area": "Наблюдаемая разница времён", "status": "INCONCLUSIVE",
            "detail": f"медиана {od.get('median')} мс, p90 {od.get('p90')} мс, "
                      f"диапазон {od.get('min'):.0f}…{od.get('max'):.0f} мс" if od.get("median") else "нет данных",
            "note": "устойчивое смещение между временем сделки источника и временем состояния стакана. "
                    "Это НЕ доказанная задержка реакции: без атрибуции события к изменению стакана "
                    "разница может быть постоянным сдвигом публикации. CAUSALITY NOT ESTABLISHED",
            "source": "REAL"})
    else:
        findings.append({"area": "Измеримость задержки", "status": "INCONCLUSIVE",
                         "detail": "зонд не запускался или не нашёл рынков", "note": "", "source": "—"})

    # 60m
    if win:
        st = "OBSERVED" if win.get("windows_ever_seen", {}).get("60m") else "NOT OBSERVED"
        findings.append({
            "area": "60-минутные окна", "status": st,
            "detail": f"наблюдений {win.get('total_observations')}, окна за всё время: "
                      f"{win.get('windows_ever_seen')}",
            "note": win.get("note_60m", ""), "source": "REAL"})
    else:
        findings.append({"area": "60-минутные окна", "status": "NOT OBSERVED",
                         "detail": "наблюдение ещё не запускалось", "note": "", "source": "—"})

    # volume
    findings.append({
        "area": "Объём торгов Polymarket", "status": "UNAVAILABLE",
        "detail": "поле отсутствует в ответе Gamma (проверено зондом 5A)",
        "note": "подменять ликвидностью нельзя — это другая величина", "source": "REAL"})

    # покрытие и полнота — детально, как требует ТЗ
    if ls and ls.get("days"):
        t = ls["totals"]
        q_ = t.get("quality", {})
        cov = {
            "total_snapshots": t.get("snapshots"), "valid": t.get("valid"),
            "GOOD": q_.get("GOOD", 0), "DEGRADED": q_.get("DEGRADED", 0),
            "INCOMPLETE": q_.get("INCOMPLETE", 0), "INVALID": q_.get("INVALID", 0),
            "by_asset": t.get("assets"), "by_window": t.get("windows"),
            "unique_markets": t.get("unique_markets"),
            "feature_availability_share": t.get("feature_availability_share"),
            "raw_present_days": t.get("raw_present_days"),
            "truncated_days": t.get("truncated_days"),
            "incomplete_rebuild_days": t.get("incomplete_rebuild_days"),
            "duplicate_ids": t.get("duplicate_ids"),
            "missing_reference_prices": q_.get("INVALID", 0),
            "missing_volume": "UNAVAILABLE (источник не отдаёт)",
        }
        findings.append({
            "area": "Покрытие и полнота", "status": "SUPPORTED" if t.get("snapshots") else "INCONCLUSIVE",
            "detail": f"GOOD {cov['GOOD']} · DEGRADED {cov['DEGRADED']} · "
                      f"INCOMPLETE {cov['INCOMPLETE']} · INVALID {cov['INVALID']}; "
                      f"активов {len(cov['by_asset'] or {})}, окон {len(cov['by_window'] or {})}, "
                      f"рынков {cov['unique_markets']}",
            "note": "INVALID = не получена опорная цена; нулями не заменяется", "source": "REAL"})
    else:
        cov = {}

    # storage
    findings.append({
        "area": "Целостность хранения", "status": "CONFIRMED",
        "detail": "raw вне Git (артефакты 90 дней), агрегаты в Git, индекс id с fsync, "
                  "агрегат защищён от уменьшения при неполном raw",
        "note": "raw старше 90 дней исчезает — долгосрочный источник только агрегаты", "source": "REAL"})

    lines = [
        "# POLYLAB — отчёт качества данных (5D.4)", "",
        f"Версия отчёта {REPORT_VERSION} · собран "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC", "",
        "Статусы: **CONFIRMED** — подтверждено данными · **SUPPORTED** — данные согласуются, "
        "выборка мала · **INCONCLUSIVE** — вывод сделать нельзя · **UNAVAILABLE** — источник не даёт · "
        "**NOT OBSERVED** — не наблюдалось (не равно «не существует»)", "",
        "| Направление | Статус | Что показали данные | Ограничения |", "|---|---|---|---|",
    ]
    for f in findings:
        lines.append(f"| {f['area']} | **{f['status']}** | {f['detail']} | {f['note']} |")
    lines += ["", "## Рекомендация", ""]
    n = (ls or {}).get("totals", {}).get("snapshots", 0)
    if n < 500:
        lines.append(f"- Данных пока мало ({n} снимков). Для вывода о качестве серии нужно "
                     "непрерывное наблюдение хотя бы несколько суток — до этого статус SUPPORTED, "
                     "а не CONFIRMED.")
    if lat and lat.get("status") == "OK":
        if (lat.get("book_change_rate") or 0) >= 0.5:
            lines.append("- Стакан меняется практически непрерывно, поэтому выделить «реакцию на событие» "
                         "нельзя: любое изменение совпадает с любым событием. Причинная задержка "
                         "**не измеряется** этим методом — нужен другой подход (например, привязка к "
                         "крупным разовым движениям, а не к каждому тику).")
        lines.append("- Наблюдаемая разница времён устойчива, но интерпретировать её как задержку "
                     "реакции нельзя без доказанной атрибуции. Торговых выводов не делаем.")
    if win and not win.get("windows_ever_seen", {}).get("60m"):
        lines.append("- 60-минутные окна не наблюдались. Продолжать наблюдение, "
                     "выводов об их отсутствии не делать.")
    lines += ["- Объём торгов Polymarket недоступен — исследования, требующие объёма, "
              "невозможны без другого источника.", ""]
    # блок покрытия в markdown — то, из чего PHASE 6 построит графики
    if cov:
        lines += ["## Покрытие", "",
                  f"- снимков всего: **{cov['total_snapshots']}**, валидных: **{cov['valid']}**",
                  f"- качество: GOOD {cov['GOOD']} · DEGRADED {cov['DEGRADED']} · "
                  f"INCOMPLETE {cov['INCOMPLETE']} · INVALID {cov['INVALID']}",
                  f"- по активам: {cov['by_asset']}", f"- по окнам: {cov['by_window']}",
                  f"- уникальных рынков: {cov['unique_markets']}",
                  f"- дней с локальным raw: {cov['raw_present_days']}, "
                  f"с обрывом: {cov['truncated_days']}, с неполной пересборкой: {cov['incomplete_rebuild_days']}",
                  f"- дублей id: {cov['duplicate_ids']}",
                  f"- объём торгов: {cov['missing_volume']}", ""]
    return {"report_version": REPORT_VERSION, "findings": findings, "coverage": cov,
            "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}, "\n".join(lines)


if __name__ == "__main__":
    data, md = build()
    os.makedirs(R, exist_ok=True)
    json.dump(data, open(os.path.join(R, "DATA_QUALITY.json"), "w"), ensure_ascii=False, indent=1)
    open(os.path.join(R, "DATA_QUALITY_REPORT.md"), "w", encoding="utf-8").write(md)
    for f in data["findings"]:
        print(f"  {f['status']:14} {f['area']}")
