# Причал AI v0.2.13 — Revenue Excludes Returns

Правило: возвраты не входят в выручку и не уменьшают её.

Возвраты остаются в базе и показываются отдельно по сумме и количеству.

Изменено: сменная, магазинная, сетевая и товарная выручка, а также reconciliation.

Reset и sync не нужны. После деплоя:

```text
/ping
/rebuildshifts 3
/shifts 2026-09-19
/reconcile 2026-09-19
```

Ожидается: `pong ✅ | v0.2.13 | revenue-excludes-returns`.
