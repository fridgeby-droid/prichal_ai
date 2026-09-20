# Причал AI v0.2.6 — Business Day Engine

Главное изменение: Причал больше не использует календарные сутки Saby
как основную управленческую дату.

## Business day

`2026-09-19`:

- начало: 19.09 08:00
- конец: 20.09 07:59:59

Внутри:

- DAY = 08:00–19:59
- NIGHT = 20:00–07:59 следующего дня

Настройка:

```env
BUSINESS_DAY_START_HOUR=8
```

## Три уровня смен

### RAW sales

Каждый чек хранит:

- `Payments.CarriedWTZ`
- `business_date`
- `business_shift_type`
- Seller
- Shift / ShiftNumber
- сумму
- возврат

### cash_shifts

Фактические кассовые сегменты Saby.

### employee_work_shifts

Рабочая/оплачиваемая смена сотрудника.

Несколько cash shifts одного сотрудника внутри одного DAY/NIGHT могут
объединяться в одну work shift.

Настройка:

```env
WORK_SHIFT_MERGE_GAP_HOURS=4
```

## Clean rebuild

Для первой установки v0.2.6 рекомендуется чистый rebuild.

ВАЖНО: временно отключите автоматическую синхронизацию:

```env
AUTO_SYNC_ENABLED=false
AUTO_SYNC_ON_START=false
```

Задеплойте v0.2.6 и выполните:

```text
/resetdata confirm
```

Затем:

```text
/sync 7
```

После загрузки:

```text
/rebuildshifts 7
```

Затем:

```text
/reconcile вчера
```

и:

```text
/shifts вчера
```

После успешной проверки можно снова включить auto sync.

## Reconciliation

`/reconcile вчера`

Для каждого магазина проверяет:

RAW business-day revenue
=
SUM(employee work shifts revenue)

и:

RAW check count
=
SUM(employee work shift check_count)

Если есть:
- денежная разница;
- потерянные чеки;
- чеки без Seller;
- REVIEW shifts;

статус магазина = REVIEW.

До PayrollEngine используем только даты со статусом OK.

## Reset safety

`/resetdata confirm` удаляет только:

- Saby raw sales/items;
- cash shifts;
- employee work shifts;
- sync history;
- stores Saby.

Причал Core и его БД не затрагиваются.
