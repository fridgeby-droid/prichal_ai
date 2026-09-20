# Причал AI v0.2

Вторая сборка директорского AI-агента.

## Что изменилось относительно v0.1

- Neon/PostgreSQL как собственная память данных;
- Saby -> PostgreSQL синхронизация;
- idempotent upsert продаж;
- хранение позиций чеков;
- seller-first ShiftEngine;
- DAY / NIGHT;
- ночная смена относится к дате её начала;
- session gap;
- confidence + AUTO / REVIEW / AMBIGUOUS;
- Saby Shift/ShiftNumber используются как дополнительное доказательство;
- история смен доступна агенту;
- live Saby tools сохранены как fallback.

## Обязательная новая переменная

```env
DATABASE_URL=postgresql://USER:PASSWORD@HOST/DB?sslmode=require
```

Можно использовать тот же Neon account, но рекомендуется отдельная БД/ветка для `prichal-ai`.

## После деплоя

1. `/ping`
2. `/db`
3. `/sync 3`
4. `/db`
5. `/shifts вчера`

Затем обычным языком:

- `Как вчера отработала сеть?`
- `Кто работал ночью вчера?`
- `Как вчера отработал Космонавтов?`
- `Сколько смен отработал Иванов в этом месяце?`

## Автосинхронизация

По умолчанию:

```env
AUTO_SYNC_ENABLED=true
AUTO_SYNC_ON_START=true
SYNC_INTERVAL_MINUTES=60
SYNC_RECENT_DAYS=3
```

Последние 3 дня пересинхронизируются, поэтому поздние изменения/возвраты Saby
обновят PostgreSQL.

## ShiftEngine

Параметры соответствуют логике, проверенной ранее в Apps Script:

```env
SHIFT_DAY_START_HOUR=8
SHIFT_NIGHT_START_HOUR=20
SHIFT_SESSION_GAP_HOURS=9
SHIFT_MAX_DURATION_HOURS=16
SHIFT_AUTO_SHARE=0.80
SHIFT_AMBIGUOUS_SHARE=0.65
```

Это пока seller shifts, а не "кассовые смены" Saby.
Если Saby заполняет Shift/ShiftNumber, они сохраняются как дополнительное
подтверждение. Если нет — смена реконструируется по активности продавца.

## Что намеренно НЕ входит в v0.2

- PayrollEngine;
- планы KPI;
- экзамены;
- write-доступ к Причал Core;
- товары каталога и остатки;
- клиентская/бонусная аналитика.

Это следующие версии.
