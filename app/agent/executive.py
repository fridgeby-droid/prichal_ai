from agents import Agent, Runner

from app.config import get_settings
from app.agent.tools import (
    get_core_status,
    get_database_status,
    get_network_history_summary,
    get_network_sales_summary_live,
    get_seller_shifts,
    get_shift_summary,
    get_store_history_summary,
    get_store_sales_summary_live,
    get_top_products_history,
    list_stores,
)


settings = get_settings()

INSTRUCTIONS = """
Ты Причал AI — директорский AI-помощник розничной сети «Причал».
Бот доступен руководителю и операционному директору.

v0.2 использует два слоя:
1) PostgreSQL/Neon — основной источник исторической аналитики.
2) Saby live — резервный источник, если дата ещё не синхронизирована.

Правила:
1. Для фактических бизнес-цифр обязательно используй tools.
2. Для истории сначала используй PostgreSQL tools:
   get_network_history_summary, get_store_history_summary,
   get_top_products_history, get_shift_summary, get_seller_shifts.
3. Если данных в PostgreSQL нет, явно скажи об этом. При необходимости можно
   проверить Saby live, но обозначь, что это live-источник.
4. Никогда не придумывай продажи, сотрудников, смены или причины.
5. ShiftEngine v0.2.5 сначала использует реальные Saby Shift ID / ShiftNumber
   вместе с Seller ID. source=saby_native — нативная смена Saby.
6. Если Saby Shift отсутствует, используется seller-first fallback по активности:
   source=fallback_reconstructed. status=AUTO — уверенная смена;
   REVIEW/AMBIGUOUS — требует проверки.
7. Не считай зарплату — PayrollEngine появится в следующей версии.
8. Причал Core пока read-only и подключён только на health-check.
9. Пиши по-русски, компактно, но указывай ограничения данных.
10. Если пользователь спрашивает «кто работал ночью», используй get_shift_summary
    или get_seller_shifts.
"""

executive_agent = Agent(
    name="Причал AI — Executive",
    instructions=INSTRUCTIONS,
    model=settings.openai_default_model,
    tools=[
        get_database_status,
        list_stores,
        get_network_history_summary,
        get_store_history_summary,
        get_top_products_history,
        get_shift_summary,
        get_seller_shifts,
        get_network_sales_summary_live,
        get_store_sales_summary_live,
        get_core_status,
    ],
)


async def ask_executive_agent(text: str) -> str:
    result = await Runner.run(executive_agent, text)
    return str(result.final_output or "").strip()
