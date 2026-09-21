# Причал AI v0.2.10 — Payment Diagnostic

Диагностическая версия. Расчёт смен не меняет.

Команда:

```text
/paymentdebug Батумская 5 | 2026-09-19
```

Показывает по каждому продавцу DAY/NIGHT:

- всю payment revenue;
- fiscal-only revenue;
- Nonfiscal amount/count;
- returns amount/count.

Отдельно выводит все `Nonfiscal=true` и `is_return=true` payment rows.

Для установки поверх v0.2.9:
- reset не нужен;
- sync не нужен;
- достаточно задеплоить и выполнить `/paymentdebug ...`.

Цель — определить, почему по Батумской:
- эталон DAY = 19 307,55 ₽;
- эталон NIGHT = 78 852,33 ₽;
- в v0.2.9 появились дополнительные 1 482 ₽ у Анферова
  и +614,90 ₽ у Лаврова.
