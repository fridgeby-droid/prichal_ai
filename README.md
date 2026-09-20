# Причал AI v0.2.1

Исправляющая сборка ShiftEngine.

## Изменения
- PostgreSQL-сессии работают в BUSINESS_TZ.
- Исторические даты считаются в Asia/Yekaterinburg, а не UTC.
- ShiftEngine строит смену по Seller ID даже если SellerName пустой.
- `/shiftdebug вчера` показывает покрытие Seller/SellerName/Teller/Shift по магазинам.
- `/rebuildshifts 4` пересобирает смены из уже загруженных данных Neon без запроса Saby.

## После деплоя
1. `/shiftdebug вчера`
2. `/rebuildshifts 4`
3. `/shifts вчера`
4. `/db`
