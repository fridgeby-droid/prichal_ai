import json

from agents.decorators import tool

from app.services.core import core_client
from app.services.saby import saby_client


def _dump(data: dict | list) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


@tool
async def list_stores() -> str:
    """Получить список магазинов Причала из Saby с pointId и адресами."""
    return _dump(await saby_client.list_points())


@tool
async def get_network_sales_summary(date: str = "вчера") -> str:
    """Получить фактическую сводку продаж по всей сети за дату.

    Args:
        date: Дата YYYY-MM-DD либо 'сегодня'/'вчера'.
    """
    return _dump(await saby_client.network_sales_summary(date))


@tool
async def get_store_sales_summary(store: str, date: str = "вчера") -> str:
    """Получить фактическую сводку продаж конкретного магазина за дату.

    Args:
        store: Название, часть названия, адрес или pointId магазина.
        date: Дата YYYY-MM-DD либо 'сегодня'/'вчера'.
    """
    return _dump(await saby_client.store_sales_summary(store, date))


@tool
async def get_top_products(
    date: str = "вчера",
    limit: int = 10,
    store: str = "",
) -> str:
    """Получить топ товаров по чистой выручке за дату.

    Args:
        date: Дата YYYY-MM-DD либо 'сегодня'/'вчера'.
        limit: Количество товаров, от 1 до 30.
        store: Необязательно. Конкретный магазин; пусто означает всю сеть.
    """
    return _dump(await saby_client.top_products(date, limit, store))


@tool
async def get_core_status() -> str:
    """Проверить доступность backend Причал Core. Не изменяет данные."""
    return _dump(await core_client.health())
