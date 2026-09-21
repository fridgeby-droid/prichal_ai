# Причал AI v0.2.11 — Version Lock

Техническая сборка без изменения бизнес-расчёта.

Исправляет путаницу версий:
- `/ping` больше не захардкожен как `v0.2`;
- `/start`, FastAPI `/`, `/health` и Telegram используют одну `APP_VERSION`;
- добавлены `/version` и `/buildinfo`;
- `/paymentdebug` помечен как `DIRECT`, чтобы было видно, что ответ пришёл
  из детерминированного SQL-handler, а не от AI-агента.

После деплоя:

```text
/ping
/version
/buildinfo
```

Ожидается:

```text
pong ✅ | v0.2.11 | payment-debug-direct
```

Затем:

```text
/paymentdebug Батумская 5 | 2026-09-19
```

Ответ должен начинаться:

```text
🧾 DIRECT Payment debug — Батумская 5
```

Reset и sync для перехода с v0.2.10 не нужны.
