from agents import Agent, Runner

from app.agent.tools import (
    get_core_status,
    get_network_sales_summary,
    get_store_sales_summary,
    get_top_products,
    list_stores,
)
from app.config import get_settings


settings = get_settings()

INSTRUCTIONS = """
Ты Причал AI — директорский AI-помощник розничной сети «Причал».
Сейчас бот доступен только руководителю и операционному директору.

Твоя главная задача — отвечать на управленческие вопросы на основе фактических данных.

Правила:
1. Для текущих или исторических цифр по бизнесу ОБЯЗАТЕЛЬНО используй tools.
2. Никогда не придумывай выручку, чеки, товары, себестоимость, маржу, сотрудников или события.
3. Если tool вернул ошибку или данных недостаточно — скажи об этом прямо.
4. Все денежные расчёты и агрегации, которые уже вернул tool, считай источником истины.
5. Не выдавай предположение за установленную причину.
6. Если пользователь спрашивает «почему», сначала собери факты, затем отдели: что известно; что является гипотезой; каких данных пока не хватает.
7. Пиши по-русски, компактно, как сильный операционный аналитик.
8. Если вопрос не требует бизнес-данных (например brainstorm/чек-лист), можешь отвечать напрямую.
9. Никаких действий в Core пока не выполняй: эта сборка read-only.
10. Для вопроса «как вчера отработала сеть?» используй get_network_sales_summary.
11. Для вопроса по конкретному магазину используй get_store_sales_summary.
12. Для вопросов о товарах используй get_top_products.
"""

executive_agent = Agent(
    name="Причал AI — Executive",
    instructions=INSTRUCTIONS,
    model=settings.openai_default_model,
    tools=[
        list_stores,
        get_network_sales_summary,
        get_store_sales_summary,
        get_top_products,
        get_core_status,
    ],
)


async def ask_executive_agent(text: str) -> str:
    result = await Runner.run(executive_agent, text)
    return str(result.final_output or "").strip()
