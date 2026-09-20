# Причал AI v0.2.7 — Payment Ledger

Исправляет источник выручки смен.

## Почему v0.2.6 могла считать смены неверно

Saby `retail/order/list` возвращает:

- `TotalPrice` — сумму ПРОДАЖИ;
- `Payments[]` — массив платежей/чеков;
- у каждого Payments:
  - `Amount` — сумма платежа;
  - `CarriedWTZ` — когда пробит чек;
  - `Shift`;
  - `ShiftNumber`;
  - `Teller`.

В v0.2.6 вся `TotalPrice` продажи привязывалась к одному check timestamp.
Если в продаже больше одного `Payments`, это неправильно для сменной выручки.

## v0.2.7

Добавлена таблица:

` sale_payments `

Одна строка = один Saby Payments[] record.

Именно она теперь является денежным и временным источником для ShiftEngine.

### Shift revenue

`SUM(sale_payments.signed_amount)`

где:
- обычная продажа: `Amount`;
- возврат: `-ABS(Amount)`.

Если Saby не вернул Payments, создаётся явный
`source=sale_total_fallback`, который будет виден в `/reconcile`.

## Business day сохраняется

Например 19.09:

19.09 08:00 → 20.09 07:59:59.

## Рекомендуемый чистый rebuild

Перед деплоем:

```env
AUTO_SYNC_ENABLED=false
AUTO_SYNC_ON_START=false
BUSINESS_DAY_START_HOUR=8
SYNC_CONCURRENCY=4
SABY_MAX_PAGES_PER_POINT=30
```

После деплоя:

```text
/resetdata confirm
```

Затем:

```text
/sync 7
```

После sync ShiftEngine пересобирается автоматически.
Дополнительно можно выполнить:

```text
/rebuildshifts 7
```

Проверка:

```text
/shiftdebug вчера
/reconcile вчера
/shifts вчера
```

## Что показывает reconcile

Три уровня:

1. `Sale TotalPrice`
2. `Payments`
3. `Employee work shifts`

Критическая проверка для зарплаты:

`Payments − Shifts = 0`

`Sale − Payments` отображается отдельно как диагностическая разница:
она помогает понять особенности данных Saby и не блокирует сама по себе
расчёт смен, если payment ledger полностью согласован с shifts.
