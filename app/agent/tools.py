import json

from agents.decorators import tool

from app.db.database import health as db_health
from app.services.analytics import analytics_service
from app.services.core import core_client
from app.services.payroll import payroll_service
from app.services.saby import saby_client
from app.services import period_reports
from app.services import catalog_categories
from app.services import management_reports


def _dump(data: dict | list) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


@tool
async def get_detail_report(kind: str, date_from: str = '', date_to: str = '', point_id: str = '',
                            seller_id: str = '', shift: str = '', query: str = '',
                            min_days: int = 0, max_days: int = -1, offset: int = 0) -> str:
    """Детальная аналитика локальных продаж без запросов Saby.

    Args:
        kind: averages (СЧ сети/точек/учёток), quantities (товары по продавцам), modifiers (только явный IsModifier=true), basket (наполняемость продажи), trend (дни/недели), clients (давность клиентов по Customer ID, НЕ карты).
        date_from: YYYY-MM-DD; обе даты пустые — вся локальная история. Для clients нижняя граница не ограничивает историю последней покупки.
        date_to: YYYY-MM-DD; для clients дата отсечения.
        point_id: Точный ID магазина или пусто для сети.
        seller_id: Точный ID учётки Saby или пусто; реальный сотрудник не подтверждён.
        shift: DAY/NIGHT или пусто. Для clients запрещён.
        query: Подстрока названия или ID товара для quantities; совпадения не объединяются.
        min_days: Минимальная давность покупки включительно для clients.
        max_days: Максимальная давность включительно; -1 без верхней границы.
        offset: Смещение страницы клиентов/товаров, по 50 групп.
    """
    from app.services.detail_reports import report
    return _dump(await report(kind,date_from,date_to,point_id,seller_id,shift,query,min_days,None if max_days==-1 else max_days,offset))


@tool
async def get_database_status() -> str:
    """Проверить PostgreSQL Timeweb и показать покрытие исторических данных."""
    return _dump(
        {
            "database": await db_health(),
            "coverage": await analytics_service.coverage(),
        }
    )


@tool
async def list_stores(query: str = "") -> str:
    """Найти магазин в локальном справочнике: название/адрес/точный ID. РЦ исключён.

    Args:
        query: Поисковая строка; пусто — все магазины. При нескольких совпадениях уточни выбор.
    """
    return _dump(await period_reports.stores(query))


@tool
async def get_period_summary(date_from: str, date_to: str, point_id: str = "") -> str:
    """Выручка, платёжные чеки, средний чек и отдельные возвраты за 1–92 бизнес-дня.

    Args:
        date_from: Начало YYYY-MM-DD, включительно.
        date_to: Конец YYYY-MM-DD, включительно.
        point_id: Точный ID из list_stores; пусто — сеть.
    """
    return _dump(await management_reports.report(date_from,date_to,point_id=point_id,kind='summary'))


@tool
async def compare_sales_periods(date_from: str, date_to: str, previous_from: str,
                                previous_to: str, point_id: str = "") -> str:
    """Сравнить два периода по сети и магазинам: абсолютные и процентные изменения.

    Args:
        date_from: Начало текущего периода YYYY-MM-DD.
        date_to: Конец текущего периода YYYY-MM-DD.
        previous_from: Начало базы сравнения YYYY-MM-DD.
        previous_to: Конец базы сравнения YYYY-MM-DD.
        point_id: Точный ID магазина; пусто — сеть.
    """
    return _dump(await management_reports.report(date_from,date_to,previous_from,previous_to,point_id,kind='comparison'))


@tool
async def get_day_night_breakdown(date_from: str, date_to: str, point_id: str = "") -> str:
    """Разбить продажи периода на DAY/NIGHT по времени платежа; не по графику работников.

    Args:
        date_from: Начало YYYY-MM-DD.
        date_to: Конец YYYY-MM-DD.
        point_id: Точный ID магазина; пусто — сеть.
    """
    return _dump(await management_reports.report(date_from,date_to,point_id=point_id,kind='daynight'))


@tool
async def get_product_sales(date_from: str, date_to: str, point_id: str = "",
                            previous_from: str = "", previous_to: str = "", limit: int = 20) -> str:
    """Продажи товаров или крупнейшие изменения товарных сумм. Для категорий есть get_category_sales.

    Args:
        date_from: Начало YYYY-MM-DD.
        date_to: Конец YYYY-MM-DD.
        point_id: Точный ID магазина; пусто — сеть.
        previous_from: Необязательное начало базы сравнения YYYY-MM-DD.
        previous_to: Необязательный конец базы сравнения YYYY-MM-DD; задаётся вместе с началом.
        limit: Количество строк, от 1 до 50.
    """
    return _dump(await management_reports.report(date_from,date_to,previous_from,previous_to,point_id,kind='products',limit=limit))


@tool
async def get_category_sales(date_from: str, date_to: str, point_id: str = "",
                             previous_from: str = "", previous_to: str = "",
                             level: str = "top", limit: int = 30) -> str:
    """Продажи по текущим разделам каталога Saby и их вклад в изменения. Не историческая классификация.

    Args:
        date_from: Начало YYYY-MM-DD.
        date_to: Конец YYYY-MM-DD.
        point_id: Точный ID магазина; пусто — сеть.
        previous_from: Начало базы сравнения; пусто — без сравнения.
        previous_to: Конец базы сравнения; задаётся вместе с предыдущим началом.
        level: top — верхний раздел, leaf — непосредственный раздел товара.
        limit: От 1 до 50 категорий.
    """
    return _dump(await management_reports.report(date_from,date_to,previous_from,previous_to,point_id,kind='categories',level=level,limit=limit))


@tool
async def get_load_reconcile_status(date_from: str, date_to: str, point_id: str = "") -> str:
    """Проверить свежесть загрузки, отсутствие записей и сверку; блок не более 7 дней.

    Args:
        date_from: Начало YYYY-MM-DD.
        date_to: Конец YYYY-MM-DD.
        point_id: Точный ID магазина; пусто — сеть.
    """
    return _dump(await management_reports.run_quality(date_from,date_to,point_id))


@tool
async def get_sales_drivers(date_from: str, date_to: str, previous_from: str,
                            previous_to: str, point_id: str = "", limit: int = 10) -> str:
    """Единый отчёт о вкладе магазинов, DAY/NIGHT, категорий и товаров в изменения двух периодов.

    Args:
        date_from: Начало текущего периода YYYY-MM-DD.
        date_to: Конец текущего периода YYYY-MM-DD.
        previous_from: Начало базы сравнения YYYY-MM-DD.
        previous_to: Конец базы сравнения YYYY-MM-DD.
        point_id: Точный ID магазина; пусто — сеть без РЦ.
        limit: Число крупнейших вкладов в каждом разрезе, от 1 до 50.
    """
    return _dump(await management_reports.report(date_from,date_to,previous_from,previous_to,point_id,kind='drivers',limit=limit))


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


@tool
async def get_shift_plan(
    store: str,
    date: str,
    shift_type: str,
) -> str:
    """Получить действующий план DAY/NIGHT для магазина и business date.

    Args:
        store: Название/часть названия/pointId.
        date: YYYY-MM-DD.
        shift_type: DAY или NIGHT.
    """
    from datetime import date as date_type

    return _dump(
        await payroll_service.resolve_plan_for_store(
            store,
            date_type.fromisoformat(date),
            shift_type,
        )
    )


@tool
async def get_payroll_policy(
    role: str,
    date: str,
) -> str:
    """Получить действующую версию зарплатной политики на дату.

    Args:
        role: SELLER или NIGHT_ASSISTANT.
        date: YYYY-MM-DD.
    """
    from datetime import date as date_type

    return _dump(
        (
            await payroll_service.resolve_policy(
                role,
                date_type.fromisoformat(date),
            )
        )
        or {
            "error": "Политика не найдена.",
            "role": role,
            "date": date,
        }
    )


@tool
async def get_seller_payroll_preview(
    date: str,
) -> str:
    """Детерминированный preview зарплаты продавцов за business date.

    Использует:
    - employee_work_shifts;
    - shift_plans;
    - versioned SELLER payroll policy.

    Exam bonus сюда не входит — он месячный.
    """
    from datetime import date as date_type

    return _dump(
        await payroll_service.seller_daily_preview(
            date_type.fromisoformat(date)
        )
    )


@tool
async def get_seller_month_payroll_preview(
    month: str,
) -> str:
    """Детерминированный месячный preview зарплаты продавцов.

    Args:
        month: YYYY-MM.
    """
    from datetime import date as date_type

    month_date = date_type.fromisoformat(
        month.strip() + "-01"
    )

    return _dump(
        await payroll_service.seller_month_preview(
            month_date
        )
    )
