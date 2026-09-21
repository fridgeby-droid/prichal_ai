import json

from agents.decorators import tool

from app.db.database import health as db_health
from app.services.analytics import analytics_service
from app.services.core import core_client
from app.services.saby import saby_client


def _dump(data: dict | list) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


@tool
async def get_database_status() -> str:
    """Проверить PostgreSQL/Neon и показать покрытие исторических данных."""
    return _dump(
        {
            "database": await db_health(),
            "coverage": await analytics_service.coverage(),
        }
    )


@tool
async def list_stores() -> str:
    """Получить список магазинов Причала из Saby с pointId и адресами."""
    return _dump(await saby_client.list_points())


@tool
async def get_network_history_summary(date: str = "вчера") -> str:
    """Получить сводку сети за Причал business day 08:00→08:00 из PostgreSQL.

    Args:
        date: Дата YYYY-MM-DD либо 'сегодня'/'вчера'.
    """
    return _dump(await analytics_service.network_day(date))


@tool
async def get_store_history_summary(store: str, date: str = "вчера") -> str:
    """Получить показатели магазина и employee work shifts за business day 08:00→08:00.

    Args:
        store: Название, часть названия, адрес или pointId.
        date: Дата YYYY-MM-DD либо 'сегодня'/'вчера'.
    """
    return _dump(await analytics_service.store_day(store, date))


@tool
async def get_top_products_history(
    date: str = "вчера",
    limit: int = 10,
    store: str = "",
) -> str:
    """Получить топ товаров из локальной истории PostgreSQL.

    Args:
        date: Дата YYYY-MM-DD либо 'сегодня'/'вчера'.
        limit: От 1 до 30.
        store: Необязательно. Магазин; пусто означает всю сеть.
    """
    return _dump(await analytics_service.top_products(date, limit, store))


@tool
async def get_shift_summary(date: str = "вчера") -> str:
    """Получить employee work shifts DAY/NIGHT за Причал business day.

    Args:
        date: Рабочая дата смены YYYY-MM-DD либо 'сегодня'/'вчера'.
    """
    return _dump(await analytics_service.shift_summary(date))


@tool
async def get_seller_shifts(
    seller: str,
    date_from: str = "",
    date_to: str = "",
) -> str:
    """Получить смены продавца за период.

    Args:
        seller: Фамилия, имя или часть имени продавца.
        date_from: Начало YYYY-MM-DD; пусто = начало текущего месяца.
        date_to: Конец YYYY-MM-DD; пусто = сегодня.
    """
    return _dump(await analytics_service.seller_shifts(seller, date_from, date_to))


# Online Saby tools from v0.1 remain as fallback/diagnostics.
@tool
async def get_network_sales_summary_live(date: str = "вчера") -> str:
    """Прямой запрос сводки в Saby без PostgreSQL. Использовать как fallback."""
    return _dump(await saby_client.network_sales_summary(date))


@tool
async def get_store_sales_summary_live(store: str, date: str = "вчера") -> str:
    """Прямой запрос магазина в Saby без PostgreSQL. Использовать как fallback."""
    return _dump(await saby_client.store_sales_summary(store, date))


@tool
async def get_core_status() -> str:
    """Проверить доступность backend Причал Core. Не изменяет данные."""
    return _dump(await core_client.health())
