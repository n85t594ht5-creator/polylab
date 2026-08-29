# Backup PROJECT B перед PHASE 5C (storage)

**Версия:** 0.1.0 · **Дата:** 2026-08-29 · **Commit:** e73169151e8a43b83e90e28388e9ce8e2291a98c
**Tag:** backup/pre-storage-5C-2026-08-29

Состояние: PHASE 0–4 + 5A + 5B завершены, тесты 99/99.
Схема данных зафиксирована тегом `baseline/schema-5A-2026-08-29`.

## Восстановление
```
git reset --hard backup/pre-storage-5C-2026-08-29
```
или покопийно из этой папки.

## Что защищать при изменениях
- детерминированный snapshot_id
- append-only индекс data/dna/*.ids (идемпотентность после обрыва gzip)
- разделение features/outcomes
- None ≠ 0

Бэкапы PROJECT A не затронуты.
