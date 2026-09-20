# Причал AI v0.2.2 — Check Time Fix

Исправляет временной источник для ShiftEngine.

Для фактической рабочей смены используется в порядке приоритета:
1. `Payments[].CarriedWTZ`
2. `Payments[].ClosedWTZ`
3. `Payments[].OpenedWTZ`
4. fallback — `DateWTZ`

`DateWTZ` отдельно хранится в `order_datetime`.
`Shift`, `ShiftNumber`, `Teller` ищутся и на уровне продажи, и внутри `Payments`.

Существующую Neon БД удалять не нужно — миграция выполняется при старте.

После деплоя:
1. `/sync 4`
2. `/shiftdebug вчера`
3. `/rebuildshifts 4`
4. `/shifts вчера`
5. `/db`
