# Обновление AI v0.3.13 → v0.3.14

Добавлен отдельный API прайсов и изображений для серверной интеграции Core.
Контракт и инструкции: [CORE-PRICE-LISTS-API.md](CORE-PRICE-LISTS-API.md).

Новые настройки: `CORE_PRICE_LISTS_API_KEY`, `SABY_PRICE_LIST_POINT_IDS`;
необязательные лимиты: `PRICE_LISTS_MAX_PAGES`, `PRICE_LISTS_TIMEOUT_SECONDS`.
Сохраняйте существующие настройки PostgreSQL, S3, продаж, графика и справочников.
Без нового ключа API прайсов закрыт. Изменений таблиц нет.

Внешняя установка и живая проверка Saby не выполнялись. После установки:

```text
python -m scripts.price_lists 23109
python -m scripts.price_lists 23109 66 --check-image
```

Новая функциональность Core в архив не входит.
