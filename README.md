# Причал AI v0.3.2 — Timeweb Clean Bootstrap

Чистая загрузка на Timeweb без копирования старой БД. Начните с [README_TIMEWEB.md](README_TIMEWEB.md). Ниже — сохранённые правила Payroll Foundation v0.3.0/v0.3.1.

## 1. Источник планов

Планы хранятся в PostgreSQL Timeweb:

`shift_plans`

Ключ:
- магазин;
- DAY/NIGHT;
- weekday или ALL;
- valid_from / valid_to.

Приоритет:
1. правило конкретного дня недели;
2. ALL;
3. внутри одинакового уровня — самая свежая `valid_from`.

Пример:

```text
/setplan Батумская 5 | NIGHT | ALL | 70000 | 2026-09-01
/setplan Батумская 5 | NIGHT | ПТ | 90000 | 2026-09-01
```

Проверка:

```text
/plan Батумская 5 | 2026-09-19 | NIGHT
/plans Батумская 5
```

## 2. Versioned payroll policy

Условия зарплаты НЕ зашиты в PayrollEngine.

Таблица:

`payroll_policies`

Seller Policy v1 автоматически создаётся с 01.07.2026:

- фикс: 2 000 ₽ / рабочая смена;
- магазинная смена <100% плана: KPI 0%;
- 100–124.99%: KPI 3%;
- >=125%: KPI 5%;
- KPI процент применяется к ЛИЧНОЙ выручке продавца;
- экзамен PASS: +3% личной месячной выручки;
- возвраты в выручку не входят.

Критическое правило:

`plan achievement = STORE DAY/NIGHT revenue / shift plan`

а НЕ:

`personal seller revenue / full store plan`.

Проверка:

```text
/paypolicy SELLER 2026-09-19
/paypolicies SELLER
```

## 3. Изменение зарплаты в будущем

Нельзя редактировать старую схему.

Например с 01.10:

```text
/setpolicy SELLER | 2026-10-01 | 2300 | 1.00=0.04;1.25=0.06 | 0.03
```

Получим Seller Policy v2.

v1 автоматически закрывается 30.09.
Пересчёт августа всегда использует v1.

## 4. Identity map

Saby seller ↔ единый сотрудник:

```text
/identitysync
```

Таблица:

`employee_identity_map`

Позже сюда добавляется:

`core_employee_id`

Для помощника:

- role=NIGHT_ASSISTANT;
- Saby seller id может быть NULL;
- Core employee id является основным источником.

## 5. Экзамен

На первом этапе можно задавать вручную:

```text
/setexam Лавров | 2026-09 | PASS
```

Позже `employee_exam_results` будет заполняться из Причал Core.

## 6. Seller PayrollEngine

Дневной preview:

```text
/payrollpreview 2026-09-19
```

Логика:

1. берём employee_work_shift;
2. считаем общую выручку магазина по DAY/NIGHT;
3. получаем shift_plan;
4. получаем payroll policy, действовавшую на эту дату;
5. определяем KPI tier;
6. начисляем:
   `base + personal_seller_revenue * KPI%`.

Экзамен в дневной расчёт не входит.

Месяц:

```text
/payrollmonth 2026-09
```

Добавляет экзаменационный бонус один раз за месяц:

`personal monthly revenue * exam_percent`

если PASS.

## 7. NIGHT_ASSISTANT

Архитектура уже поддерживает роль:

`NIGHT_ASSISTANT`

Но v0.3.0 НЕ создаёт им смены из Saby.

Правильный следующий слой:

Причал Core
→ assistant shifts
→ employee_identity_map
→ NIGHT_ASSISTANT policy
→ PayrollEngine

То есть помощники не будут искусственно появляться в Saby shifts.

## Зарплатный этап — после завершения initial load и reconcile

```text
/ping
/identitysync
/paypolicy SELLER 2026-09-19
```

Затем заведите планы нескольких контрольных точек:

```text
/setplan Батумская 5 | DAY | ALL | <ПЛАН> | 2026-09-01
/setplan Батумская 5 | NIGHT | ALL | <ПЛАН> | 2026-09-01
```

После этого:

```text
/payrollpreview 2026-09-19
```

Когда планы заново заведены полностью:

```text
/payrollmonth 2026-09
```

Следующий этап:
- массовый импорт планов;
- Core adapter для NIGHT_ASSISTANT;
- отдельная versioned assistant policy.
